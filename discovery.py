import argparse
import errno
import json
import os
import re
import requests
import sys
import time
from functools import partial


def flink_cluster_overview(jm_url):
    r = requests.get(jm_url+'/overview')
    if r.status_code != 200:
        return {}
    decoded = r.json()
    return decoded


def flink_jobmanager_prometheus_addr(jm_url):
    addr = None
    port = None

    r = requests.get(jm_url+'/jobmanager/config')
    if r.status_code != 200:
        return ''
    dic = {}
    for obj in r.json():
        dic[obj['key']] = obj['value']
    addr = dic['jobmanager.rpc.address']

    r = requests.get(jm_url + '/jobmanager/log', stream=True)
    if r.status_code != 200:
        return ''
    for line in r.iter_lines(decode_unicode=True):
        if "Started PrometheusReporter HTTP server on port" in line:
            m = re.search('on port (\d+)', line)
            if m:
                port = m.group(1)
                break

    cond1 = addr is not None
    cond2 = port is not None
    if cond1 and cond2:
        return addr+':'+port
    else:
        return ''


def flink_taskmanager_prometheus_addr(tm_id, jm_url, version):
    addr = None
    port = None

    r = requests.get(jm_url+'/taskmanagers/'+tm_id+'/log', stream=True)
    if r.status_code != 200:
        return ''

    for line in r.iter_lines(decode_unicode=True):
        if "hostname/address" in line:
            if version.startswith("1.4"):
                m = re.search("address '([0-9A-Za-z-_]+)' \([\d.]+\)", line)
                if m:
                    hostname = m.group(1)
                    addr = hostname
            elif version.startswith("1.5") or version.startswith("1.6"):
                m = re.search("TaskManager: ([0-9A-Za-z-_]+)", line)
                if m:
                    hostname = m.group(1)
                    addr = hostname

        if "Started PrometheusReporter HTTP server on port" in line:
            m = re.search('on port (\d+)', line)
            if m:
                port = m.group(1)

        cond1 = addr is not None
        cond2 = port is not None
        if cond1 and cond2:
            return addr+':'+port

    return ''


def yarn_application_info(app_id, rm_addr):
    r = requests.get(rm_addr + '/ws/v1/cluster/apps/' + app_id)
    if r.status_code != 200:
        return {}

    decoded = r.json()
    return decoded['app'] if 'app' in decoded else {}


def taskmanager_ids(jm_url):
    r = requests.get(jm_url + '/taskmanagers')
    if r.status_code != 200:
        return []

    decoded = r.json()
    if 'taskmanagers' not in decoded:
        return []

    return [tm['id'] for tm in decoded['taskmanagers']]


def prometheus_addresses(app_id, rm_addr):
    prom_addrs = []
    while True:
        app_info = yarn_application_info(app_id, rm_addr)
        if 'trackingUrl' not in app_info:
            time.sleep(1)
            continue

        jm_url = app_info['trackingUrl']
        jm_url = jm_url[:-1] if jm_url.endswith('/') else jm_url

        overview = flink_cluster_overview(jm_url)
        version = overview['flink-version']
        taskmanagers = overview['taskmanagers']

        if app_info['runningContainers'] == 1:
            print("runningContainers(%d) is 1" % (app_info['runningContainers'],))
            time.sleep(1)
            continue

        if app_info['runningContainers'] != taskmanagers+1:
            print("runningContainers(%d) != jobmanager(1)+taskmanagers(%d)" % (app_info['runningContainers'], taskmanagers))
            time.sleep(1)
            continue

        tm_ids = taskmanager_ids(jm_url)
        prom_addrs = map(partial(flink_taskmanager_prometheus_addr, jm_url=jm_url, version=version), tm_ids)
        prom_addrs = list(filter(lambda x: len(x) > 0, prom_addrs))
        if len(tm_ids) != len(prom_addrs):
            print("Not all taskmanagers open prometheus endpoints. %d of %d opened" % (len(tm_ids), len(prom_addrs)))
            time.sleep(1)
            continue
        break

    while True:
        jm_prom_addr = flink_jobmanager_prometheus_addr(jm_url)
        if len(jm_prom_addr) == 0:
            time.sleep(1)
            continue
        prom_addrs.append(jm_prom_addr)
        break

    encoded = json.JSONEncoder().encode([{'targets': prom_addrs}])
    return encoded


def main():
    parser = argparse.ArgumentParser(description='Discover Flink clusters on Hadoop YARN for Prometheus')
    parser.add_argument('rm_addr', type=str,
                        help='(required) Specify yarn.resourcemanager.webapp.address of your YARN cluster.')
    parser.add_argument('--app-id', type=str,
                        help='If specified, this program runs once for the application. '
                             'Otherwise, it runs as a service.')
    parser.add_argument('--name-filter', type=str,
                        help='A regex to specify applications to watch.')
    parser.add_argument('--target-dir', type=str,
                        help='If specified, this program writes the target information to a file on the directory. '
                             'Files are named after the application ids. '
                             'Otherwise, it prints to stdout.')
    parser.add_argument('--poll-interval', type=int, default=5,
                        help='Polling interval to YARN in seconds '
                             'to check applications that are newly added or recently finished. '
                             'Default is 5 seconds.')

    args = parser.parse_args()
    app_id = args.app_id
    name_filter_regex = None if args.name_filter is None else re.compile(args.name_filter)
    rm_addr = args.rm_addr if "://" in args.rm_addr else "http://" + args.rm_addr
    rm_addr = rm_addr[:-1] if rm_addr.endswith('/') else rm_addr
    target_dir = args.target_dir

    if target_dir is not None and not os.path.isdir(target_dir):
        print('cannot find', target_dir)
        sys.exit(1)

    if app_id is not None:
        target_string = prometheus_addresses(app_id, rm_addr)
        if target_dir is not None:
            path = os.path.join(target_dir, app_id+".json")
            with open(path, 'w') as f:
                print(path + " : " + target_string)
                f.write(target_string)
        else:
            print(target_string)
    else:
        print("start polling every " + str(args.poll_interval) + " seconds.")
        running_prev = None
        while True:
            running_cur = {}
            added = set()
            removed = set()

            r = requests.get(rm_addr+'/ws/v1/cluster/apps')
            if r.status_code != 200:
                print("Failed to connect to the server")
                print("The status code is " + r.status_code)
                break

            decoded = r.json()
            apps = decoded['apps']['app']
            if name_filter_regex is not None:
                apps = list(filter(lambda app: name_filter_regex.match(app['name']), apps))
            for app in apps:
                if app['state'].lower() == 'running':
                    running_cur[app['id']] = app

            if running_prev is not None:
                added = set(running_cur.keys()) - set(running_prev.keys())
                removed = set(running_prev.keys()) - set(running_cur.keys())

            if len(added) + len(removed) > 0:
                print('====', time.strftime("%c"), '====')
                print('# running apps : ', len(running_cur))
                print('# added        : ', added)
                print('# removed      : ', removed)

                for app_id in added:
                    target_string = prometheus_addresses(app_id, rm_addr)
                    if target_dir is not None:
                        path = os.path.join(target_dir, app_id + ".json")
                        with open(path, 'w') as f:
                            print(path, " : ", target_string)
                            f.write(target_string)
                    else:
                        print(target_string)

                for app_id in removed:
                    if target_dir is not None:
                        path = os.path.join(target_dir, app_id + ".json")
                        print(path + " deleted")
                        try:
                            os.remove(path)
                        except OSError as e:
                            if e.errno != errno.ENOENT:
                                # re-raise exception if a different error occurred
                                raise

            running_prev = running_cur
            time.sleep(args.poll_interval)


if __name__ == '__main__':
    main()

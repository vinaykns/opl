#!/usr/bin/env python3

import argparse
import datetime
import json
import logging
import os
import sys
import tempfile

import opl.status_data

import requests

import tabulate


def _es_get_test(args, key, val, size=1):
    url = f"{args.es_server}/{args.es_index}/_search"
    headers = {
        'Content-Type': 'application/json',
    }
    data = {
        "query": {
            "bool": {
                "filter": [
                    {
                        "term": {
                            key: val,
                        },
                    },
                ],
            },
        },
        "sort": {
            "started": {
                "order": "desc",
            },
        },
        "size": size,
    }

    logging.info(f"Querying ES with url={url}, headers={headers} and json={json.dumps(data)}")
    response = requests.get(url, headers=headers, json=data)
    response.raise_for_status()
    logging.debug(f"Got back this: {json.dumps(response.json(), sort_keys=True, indent=4)}")

    return response.json()


def doit_list(args):
    assert args.list_name is not None

    response = _es_get_test(args, "name.keyword", args.list_name, args.list_size)

    table_headers = [
        'Run ID',
        'Started',
        'Owner',
        'Golden',
        'Result',
    ]
    table = []

    for item in response['hits']['hits']:
        logging.debug(f"Loading data from document ID {item['_id']} with field id={item['_source']['id'] if 'id' in item['_source'] else None}")
        tmpfile = tempfile.NamedTemporaryFile(prefix=item['_id'], delete=False).name
        sd = opl.status_data.StatusData(tmpfile, data=item['_source'])
        table.append([
            sd.get('id'),
            sd.get('started'),
            sd.get('owner'),
            sd.get('golden'),
            sd.get('result'),
        ])

    print(tabulate.tabulate(table, headers=table_headers))


def doit_change(args):
    assert args.change_id is not None

    response = _es_get_test(args, "id.keyword", args.change_id)

    source = response['hits']['hits'][0]
    es_type = source['_type']
    es_id = source['_id']
    logging.debug(f"Loading data from document ID {source['_id']} with field id={source['_source']['id']}")
    tmpfile = tempfile.NamedTemporaryFile(prefix=source['_id'], delete=False).name
    sd = opl.status_data.StatusData(tmpfile, data=source['_source'])

    for item in args.change_set:
        if item == '':
            logging.warning("Got empty key=value pair to set - ignoring it")
            continue

        key, value = item.split('=')

        try:
            value = int(value)
        except ValueError:
            try:
                value = float(value)
            except ValueError:
                pass

        logging.debug(f"Setting {key} = {value} ({type(value)})")
        sd.set(key, value)

    # Add comment to log the change
    if sd.get('comments') is None:
        sd.set('comments', [])
    if not isinstance(sd.get('comments'), list):
        logging.error(f"Field 'comments' is not a list: {sd.get('comments')}")
    if args.change_comment_text is None:
        args.change_comment_text = 'Setting ' + ', '.join(args.change_set)
    sd.get('comments').append({
        'author': os.getenv('USER', 'unknown'),
        'date': datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc).isoformat(),
        'text': args.change_comment_text,
    })

    url = f"{args.es_server}/{args.es_index}/{es_type}/{es_id}"

    logging.info(f"Saving to ES with url={url} and json={json.dumps(sd.dump())}")

    response = requests.post(url, json=sd.dump())
    response.raise_for_status()
    logging.debug(f"Got back this: {json.dumps(response.json(), sort_keys=True, indent=4)}")

    print(sd.info())


def doit_rp_to_es(args):
    assert args.es_server is not None
    assert args.rp_host is not None

    RP_TO_ES_STATE = {
        "automation_bug": "FAIL",
        "no_defect": "PASS",
        "product_bug": "FAIL",
        "system_issue": "ERROR",
        "to_investigate": "FAIL",
    }

    # Start a session
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {args.rp_token}',
    }
    session = requests.Session()

    # Get 10 newest launches
    url = f'https://{args.rp_host}/api/v1/{args.rp_project}/launch'
    data = {
      "filter.eq.name": args.rp_launch,
      "page.size": 10,
      "page.sort": "endTime,desc",
    }
    logging.debug(f"Going to do GET request to {url} with {data}")
    response = session.get(url, params=data, headers=headers, verify=not args.rp_noverify)
    if not response.ok:
        logging.error(f"Request failed: {response.text}")
    response.raise_for_status()
    logging.debug(f"Request returned {response.json()}")
    launches = response.json()['content']

    for launch in launches:
        # Get run ID from launch attributes
        run_id = None
        for a in launch["attributes"]:
            if a["key"] == "run_id":
                run_id = a["value"]
                break
        if run_id is None:
            logging.warning(f"Launch id={launch['id']} do not have run_id key, skipping it")
            continue

        # Validate defects in launch
        if sum([d["total"] for d in launch["statistics"]["defects"].values()]) != 1:
            logging.warning(f"Launch id={launch['id']} do not have expected number of defects, skipping it")
            continue
        if len(list(launch["statistics"]["defects"].keys())) != 1:
            logging.warning(f"Launch id={launch['id']} do not have expected number of defect types, skipping it")
            continue

        # Get resuls from launch statistics
        result = RP_TO_ES_STATE[list(launch["statistics"]["defects"].keys())[0]]

        # Get tests in the launch
        response = _es_get_test(args, "id.keyword", run_id)
        source = response['hits']['hits'][0]
        es_type = source['_type']
        es_id = source['_id']
        logging.debug(f"Loading data from document ID {source['_id']} with field id={source['_source']['id']}")
        tmpfile = tempfile.NamedTemporaryFile(prefix=source['_id'], delete=False).name
        sd = opl.status_data.StatusData(tmpfile, data=source['_source'])

        if sd.get("result") != result:
            logging.info(f"Results do not match, updating them: {sd.get('result')} != {result}")
            sd.set("result", result)

            # Add comment to log the change
            if sd.get('comments') is None:
                sd.set('comments', [])
            if not isinstance(sd.get('comments'), list):
                logging.error(f"Field 'comments' is not a list: {sd.get('comments')}")
                raise Exception(f"Field 'comments' is not a list: {sd.get('comments')}")
            sd.get('comments').append({
                'author': "status_data_updater",
                'date': datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc).isoformat(),
                'text': "Automatic update as per ReportPortal change",
            })

            # Save the changes to ES
            url = f"{args.es_server}/{args.es_index}/{es_type}/{es_id}"
            logging.info(f"Saving to ES with url={url} and json={json.dumps(sd.dump())}")
            response = requests.post(url, json=sd.dump())
            response.raise_for_status()
            logging.debug(f"Got back this: {json.dumps(response.json(), sort_keys=True, indent=4)}")


def main():
    parser = argparse.ArgumentParser(
        description='Investigate and modify status data documents in ElasticSearch',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument('--action', required=True,
                        choices=['list', 'change', 'rp-to-es'],
                        help='What action to do')

    parser.add_argument('--es-server',
                        default='http://elasticsearch.example.com:9286',
                        help='ElasticSearch server for the results data')
    parser.add_argument('--es-index', default='my-index',
                        help='ElasticSearch index for the results data')

    parser.add_argument('--list-name',
                        help='Name of the test to query for when listing')
    parser.add_argument('--list-size', type=int, default=50,
                        help='Number of documents to show when listing')

    parser.add_argument('--change-id',
                        help='ID of a test run when changing')
    parser.add_argument('--change-set', nargs='*', default=[],
                        help='Set key=value data')
    parser.add_argument('--change-comment-text',
                        help='Comment to be added as part of change')

    parser.add_argument('--rp-host',
                        help='ReportPortal host')
    parser.add_argument('--rp-noverify', action='store_true',
                        help='When talking to ReportPortal ignore certificate verification failures')
    parser.add_argument('--rp-project',
                        help='ReportPortal project')
    parser.add_argument('--rp-token',
                        help='ReportPortal token')
    parser.add_argument('--rp-launch',
                        help='ReportPortal launch name')

    parser.add_argument('-d', '--debug', action='store_true',
                        help='Show debug output')
    args = parser.parse_args()

    if args.debug:
        logging.basicConfig(level=logging.DEBUG)

    logging.debug(f"Args: {args}")

    if args.action == 'list':
        return doit_list(args)
    if args.action == 'change':
        return doit_change(args)
    if args.action == 'rp-to-es':
        return doit_rp_to_es(args)

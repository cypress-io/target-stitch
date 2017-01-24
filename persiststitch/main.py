import argparse
import logging
import os
import copy
import io
import sys
import time
import json
from datetime import datetime
from dateutil import tz

from strict_rfc3339 import rfc3339_to_timestamp

from jsonschema import Draft4Validator, validators, FormatChecker
from stitchclient.client import Client

logger = logging.getLogger()


class DryRunClient(object):
    """A client that doesn't actually persist to the Gate.

    Useful for testing.
    """

    def __init__(self, callback_function):
        self.callback_function = callback_function
        self.pending_callback_args = []

    def flush(self):
        logger.info("---- DRY RUN: NOTHING IS BEING PERSISTED TO STITCH ----")
        self.callback_function(self.pending_callback_args)
        self.pending_callback_args = []

    def push(self, message, callback_arg=None):
        self.pending_callback_args.append(callback_arg)

        if len(self.pending_callback_args) % 100 == 0:
            self.flush()

    def __enter__(self):
        return self

    def __exit__(self, exception_type, exception_value, traceback):
        self.flush()


# TODO: Maybe we'll put this in a shared lib
def configure_logging(level=logging.DEBUG,foo={}):
    global logger
    logger.setLevel(level)
    ch = logging.StreamHandler()
    ch.setLevel(level)
    formatter = logging.Formatter('%(levelname)s - %(message)s')
    ch.setFormatter(formatter)
    logger.addHandler(ch)


def extend_with_default(validator_class):
    validate_properties = validator_class.VALIDATORS["properties"]

    def set_defaults(validator, properties, instance, schema):
        for error in validate_properties(
            validator, properties, instance, schema,
        ):
            yield error

        for property, subschema in properties.items():
            if "format" in subschema:
                if (subschema['format'] == 'date-time' and
                        property in instance and
                        instance[property] is not None):
                    try:
                        instance[property] = datetime.fromtimestamp(
                            rfc3339_to_timestamp(instance[property])
                        ).replace(tzinfo=tz.tzutc())
                    except Exception as e:
                        raise Exception('Error parsing property {}, value {}'
                                        .format(property, instance[property]))

    return validators.extend(
        validator_class, {"properties": set_defaults},
    )


def parse_key_fields(stream_name, schemas):
    if (stream_name in schemas and
            'properties' in schemas[stream_name]):
        return [k for (k, v) in schemas[stream_name]['properties'].items()
                if 'key' in v and v['key'] is True]
    else:
        return []


def parse_record(full_record, schemas):
    try:
        stream_name = full_record['stream']
        if stream_name in schemas:
            schema = schemas[stream_name]
        else:
            schema = {}
        o = copy.deepcopy(full_record['record'])
        v = extend_with_default(Draft4Validator)
        v(schema, format_checker=FormatChecker()).validate(o)
        return o
    except:
        raise Exception("Error parsing record {}".format(full_record))


def push_state(states):
    """Called with the list of states associated with the messages that we
just persisted to the gate. These states will often be None.

    """
    logger.info('Persisted batch of {} records to Stitch'.format(len(states)))
    for state in states:
        if state is not None:
            logger.debug('Emitting state {}'.format(state))
            sys.stdout.write("{}\n".format(state))
            sys.stdout.flush()
            

def persist_lines(stitchclient, lines):
    """Takes a client and a stream and persists all the records to the gate,
printing the state to stdout after each batch."""
    state = None
    schemas = {}
    for line in lines:
        o = json.loads(line)
        if o['type'] == 'RECORD':
            message = {'action': 'upsert',
                       'table_name': o['stream'],
                       'key_names': parse_key_fields(o['stream'], schemas),
                       'sequence': int(time.time() * 1000),
                       'data': parse_record(o, schemas)}
            stitchclient.push(message, state)
            state = None
        elif o['type'] == 'STATE':
            logger.debug('Setting state to {}'.format(o['value']))
            state = o['value']
        elif o['type'] == 'SCHEMA':
            schemas[o['stream']] = o['schema']
        else:
            raise Exception("Unknown message type {} in message {}"
                            .format(o['type'], o))
    return state


def stitch_client(args):
    """Returns an instance of StitchClient or DryRunClient"""
    if args.dry_run:
        return DryRunClient(callback_function=push_state)
    else:
        with open(args.config) as input:
            config = json.load(input)
        missing_fields = []

        if 'client_id' in config:
            client_id = config['client_id']
        else:
            missing_fields.append('client_id')

        if 'token' in config:
            token = config['token']
        else:
            missing_fields.append('token')
        
        if len(missing_fields) > 0:
            raise Exception('Configuration is missing required fields: {}'
                            .format(missing_fields))
        return Client(client_id, token, callback_function=push_state)


def do_sync(args):
    input = io.TextIOWrapper(sys.stdin.buffer, encoding='utf-8')
    state = None
    with stitch_client(args) as client:
        state = persist_lines(client, input)
    if state is not None:
        logger.debug('Emitting final state {}'.format(state))
        print(state)
    

def main():

    parser = argparse.ArgumentParser()

    subparsers = parser.add_subparsers()
    
    parser_sync = subparsers.add_parser('sync')
    parser_sync.set_defaults(func=do_sync)

    for subparser in [parser_sync]:
        subparser.add_argument('-c', '--config', help='Config file', required=True)
    parser_sync.add_argument('-n',
                             '--dry-run',
                             help='Dry run - Do not push data to Stitch',
                             action='store_true')
    
    args = parser.parse_args()
    configure_logging()

    if 'func' in args:
        args.func(args)
    else:
        parser.print_help()
        exit(1)

    args = parser.parse_args()

    configure_logging()



if __name__ == '__main__':
    main()

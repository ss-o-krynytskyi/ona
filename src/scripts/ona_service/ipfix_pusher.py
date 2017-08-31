#  Copyright 2015 Observable Networks
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from __future__ import print_function, unicode_literals

# python builtins
import io
import logging

from csv import DictReader, DictWriter
from datetime import datetime
from gzip import open as gz_open
from os import environ, remove
from os.path import basename, join, split
from shutil import copy
from subprocess import call

# local
from pusher import Pusher
from utils import timestamp

FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
logging.basicConfig(level=logging.INFO, format=FORMAT)

ENV_IPFIX_LOGDIR = 'OBSRVBL_IPFIX_LOGDIR'
DEFAULT_IPFIX_LOGDIR = './logs'

ENV_MONITOR_NETS = 'OBSRVBL_NETWORKS'
DEFAULT_MONITOR_NETS = "10.0.0.0/8 172.16.0.0/12 192.168.0.0/16"

ENV_IPFIX_INDEX_RANGES = 'OBSRVBL_IPFIX_INDEX_RANGES'

CSV_HEADER = 'srcaddr,dstaddr,srcport,dstport,protocol,bytes,packets,start,end'
RWFILTER_PATH = '/opt/silk/bin/rwfilter'
RWUNIQ_PATH = '/opt/silk/bin/rwuniq'
POLL_SECONDS = 30


def get_index_filter(ranges_str):
    """
    Given strings like '0-5,7-10', return a string of comma-separated integers
    that span the range. Integers must be between 0 and 65535 inclusive.

    Example: '0-5' -> 0,5
    Example: '0-5,7-10' -> '0,1,2,4,5,7,8,9,10'
    """
    index_filter = []
    for index_range in ranges_str.split(','):
        range_parts = index_range.strip().split('-')
        if len(range_parts) != 2:
            continue
        min_index, max_index = int(range_parts[0]), int(range_parts[1])
        if not (min_index < max_index <= 65535):
            continue
        for i in xrange(min_index, max_index + 1):
            index_filter.append(str(i))

    return ','.join(index_filter)


class IPFIXPusher(Pusher):
    """Combines IPFIX data into 10 minute segments and send them to
    Observable Networks.
    """

    def __init__(self, *args, **kwargs):
        for key, default in (
            ('file_fmt', '%Y%m%d%H%M'),
            ('prefix_len', 12),
            ('data_type', 'ipfix'),
            ('input_dir', environ.get(ENV_IPFIX_LOGDIR, DEFAULT_IPFIX_LOGDIR)),
            ('poll_seconds', POLL_SECONDS),
        ):
            kwargs.setdefault(key, default)

        monitor_nets = environ.get(ENV_MONITOR_NETS, DEFAULT_MONITOR_NETS)
        self.net_filter = monitor_nets.replace(' ', ',')

        index_ranges = environ.get(ENV_IPFIX_INDEX_RANGES, '')
        self.index_filter = get_index_filter(index_ranges)

        environ['SILK_CLOBBER'] = 'true'
        environ['TZ'] = 'Etc/UTC'

        self.tar_mode = 'w'

        super(IPFIXPusher, self).__init__(*args, **kwargs)

    def _filter_silk(self, input_path, output_path):
        command = [
            RWFILTER_PATH,
            '--pass-destination', output_path,
            '--any-cidr', self.net_filter,
        ]

        if self.index_filter:
            command.append('--any-index')
            command.append(self.index_filter)

        command.append(input_path)

        return_code = call(command)
        if return_code:
            logging.warning('rwfilter error processing %s', input_path)
            return False

        return True

    def _aggregate_silk(self, input_path, output_path):
        command = [
            RWUNIQ_PATH,
            '--no-titles',
            '--no-columns',
            '--no-final-delimiter',
            '--sort-output',
            '--column-sep', ',',
            '--timestamp-format', 'epoch',
            '--fields', 'sIp,dIp,sPort,dPort,protocol',
            '--values', 'Bytes,Packets,sTime-Earliest,eTime-Latest',
            '--output-path', output_path,
            input_path,
        ]
        return_code = call(command)
        if return_code:
            logging.warning('rwuniq error processing %s', input_path)
            return False

        return True

    def _change_timestamps(self, row, ts_received):
        row['start'] = ts_received
        row['end'] = ts_received

        return row

    def _swap_directions(self, row):
        row['srcaddr'], row['dstaddr'] = row['dstaddr'], row['srcaddr']
        row['srcport'], row['dstport'] = row['dstport'], row['srcport']

        return row

    def _match_zero_protocol(self, rows):
        # List-ify the rows iterable, since we need it twice
        in_rows = list(rows)

        # Read through the flows, mapping 4-tuple to protocol (last one wins)
        protocol_map = {}
        for row in in_rows:
            key = (
                row['srcaddr'], row['dstaddr'], row['srcport'], row['dstport']
            )
            if row['protocol'] != '0':
                protocol_map[key] = row['protocol']

        # Read through the flows again. For those that have a 0 protocol,
        # see if the reverse flow is known, and if so, replace it with that one
        for row in in_rows:
            if row['protocol'] == '0':
                reverse_key = (
                    row['dstaddr'],
                    row['srcaddr'],
                    row['dstport'],
                    row['srcport']
                )
                row['protocol'] = protocol_map.get(reverse_key, '0')

            yield row

    def _get_quirks(self, input_path):
        # The input_path is like '/path/to/20170428150641_Sindex.000000.tmp'
        # Pull out the index
        probe_index = basename(input_path).split('_')[1].split('.', 1)[0][1:]
        key = 'OBSRVBL_IPFIX_PROBE_{}_SOURCE'.format(probe_index)
        source = environ.get(key)

        # Set per-source quirks
        ret = {}
        if source == 'asa':
            ret['fix_zero_protocol'] = True
        elif source == 'sonicwall':
            ret['replace_timestamps'] = True
        elif source == 'meraki':
            ret['replace_timestamps'] = True
            ret['reverse_directions'] = True

        return ret

    def _get_received_datetime(self, file_path):
        file_name = basename(file_path)
        prefix = file_name[:self.prefix_len]

        return datetime.strptime(prefix, self.file_fmt)

    def _silk_to_csv(self, input_path, output_path):
        ts_received = timestamp(self._get_received_datetime(input_path))

        quirks = self._get_quirks(input_path)

        in_args = input_path, 'rt'
        out_args = output_path, 'wt'
        fieldnames = CSV_HEADER.split(',')
        with io.open(*in_args) as infile, gz_open(*out_args) as outfile:
            csv_reader = DictReader(infile, fieldnames=fieldnames)
            csv_writer = DictWriter(
                outfile, fieldnames=fieldnames, lineterminator='\n'
            )
            csv_writer.writeheader()
            rows = csv_reader
            # If the timestamps from the NetFlow source are not trustworthy,
            # replace them with the received time.
            if quirks.get('replace_timestamps'):
                rows = (self._change_timestamps(r, ts_received) for r in rows)
            # If the directions from the NetFlow source are backward,
            # reverse them
            if quirks.get('reverse_directions'):
                rows = (self._swap_directions(r) for r in rows)
            # If the NetFlow source writes 0 for the protocol, try to fix it
            if quirks.get('fix_zero_protocol'):
                rows = self._match_zero_protocol(rows)

            csv_writer.writerows(rows)

    def _process_files(self, file_list):
        for file_path in file_list:
            file_dir, file_name = split(file_path)
            temp_path = join(file_dir, '{}.tmp'.format(file_name))
            copy(file_path, temp_path)

            self._filter_silk(temp_path, file_path)
            self._aggregate_silk(file_path, temp_path)
            self._silk_to_csv(temp_path, file_path)

            remove(temp_path)


if __name__ == '__main__':
    pusher = IPFIXPusher()
    pusher.run()

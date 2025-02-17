#!/usr/bin/env python3
import argparse
import collections
import csv
import datetime
import decimal
import re
import shlex
import subprocess
import sys

device_info_re = re.compile(r'^(?P<k>[^:]+?)(?:(?:\sis|):)\s*(?P<v>.*)$')

ata_error_count_re = re.compile(
    r'^Error (\d+) \[\d+\] occurred', re.MULTILINE)

self_test_re = re.compile(r'^SMART.*(PASSED|OK)$', re.MULTILINE)

device_info_map = {
    'Vendor': 'vendor',
    'Product': 'product',
    'Revision': 'revision',
    'Logical Unit id': 'lun_id',
    'Model Family': 'model_family',
    'Device Model': 'device_model',
    'Serial Number': 'serial_number',
    'Firmware Version': 'firmware_version',
}

smart_attributes_whitelist = {
    'airflow_temperature_cel',
    'command_timeout',
    'current_pending_sector',
    'end_to_end_error',
    'erase_fail_count_total',
    'g_sense_error_rate',
    'hardware_ecc_recovered',
    'host_reads_mib',
    'host_reads_32mib',
    'host_writes_mib',
    'host_writes_32mib',
    'load_cycle_count',
    'lifetime_writes_gib',
    'media_wearout_indicator',
    'wear_leveling_count',
    'nand_writes_1gib',
    'offline_uncorrectable',
    'power_cycle_count',
    'power_on_hours',
    'program_fail_count',
    'raw_read_error_rate',
    'reallocated_event_count',
    'reallocated_sector_ct',
    'reported_uncorrect',
    'sata_downshift_count',
    'seek_error_rate',
    'spin_retry_count',
    'spin_up_time',
    'start_stop_count',
    'temperature_case',
    'temperature_celsius',
    'temperature_internal',
    'total_bad_block',
    'total_lbas_read',
    'total_lbas_written',
    'total_writes_gib',
    'total_reads_gib',
    'udma_crc_error_count',
    'unsafe_shutdown_count',
    'unexpect_power_loss_ct',
    'workld_host_reads_perc',
    'workld_media_wear_indic',
    'workload_minutes',
}

Metric = collections.namedtuple('Metric', 'name labels value')

SmartAttribute = collections.namedtuple('SmartAttribute', [
    'id', 'name', 'flag', 'value', 'worst', 'threshold', 'type', 'updated',
    'when_failed', 'raw_value',
])


class Device(collections.namedtuple('DeviceBase', 'path opts')):
    """Representation of a device as found by smartctl --scan output."""

    @property
    def type(self):
        return self.opts.type

    @property
    def base_labels(self):
        return {'device': self.path, 'disk': self.type.partition('+')[2] or '0'}

    def smartctl_select(self):
        return ['--device', self.type, self.path]


def metric_key(metric, prefix=''):
    return '{prefix}{metric.name}'.format(prefix=prefix, metric=metric)


def metric_format(metric, prefix=''):
    key = metric_key(metric, prefix)
    labels = ','.join(
        '{k}="{v}"'.format(k=k, v=v.replace('"', '\\"')) for k, v in metric.labels.items())
    value = decimal.Decimal(metric.value)

    return '{key}{{{labels}}} {value}'.format(
        key=key, labels=labels, value=value)


def metric_print_meta(metric, prefix=''):
    key = metric_key(metric, prefix)
    print('# HELP {key} SMART metric {metric.name}'.format(
        key=key, metric=metric))
    print('# TYPE {key} gauge'.format(key=key))


def metric_print(metric, prefix=''):
    print(metric_format(metric, prefix))


def smart_ctl(*args, check=True):
    """Wrapper around invoking the smartctl binary.

    Returns:
        (str) Data piped to stdout by the smartctl subprocess.
    """
    return subprocess.run(
        ['smartctl', *args], stdout=subprocess.PIPE, check=check
    ).stdout.decode('utf-8')


def smart_ctl_version():
    return smart_ctl('-V').split('\n')[0].split()[1]


def find_devices():
    """Find SMART devices.

    Yields:
        (Device) Single device found by smartctl.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument('-d', '--device', dest='type')

    devices = smart_ctl('-d', 'by-id', '--scan-open')

    for device in devices.split('\n'):
        device = device.strip()
        if not device:
            continue

        tokens = shlex.split(device, comments=True)
        if not tokens:
            continue

        yield Device(tokens[0], parser.parse_args(tokens[1:]))


def device_is_active(device):
    """Returns whenever the given device is currently active or not.

    Args:
        device: (Device) Device in question.

    Returns:
        (bool) True if the device is active and False otherwise.
    """
    try:
        smart_ctl('--nocheck', 'standby', *device.smartctl_select())
    except subprocess.CalledProcessError:
        return False

    return True


def device_info(device):
    """Query device for basic model information.

    Args:
        device: (Device) Device in question.

    Returns:
        (generator): Generator yielding:

            key (str): Key describing the value.
            value (str): Actual value.
    """
    info_lines = smart_ctl(
        '--info', *device.smartctl_select()
    ).strip().split('\n')[3:]

    matches = (device_info_re.match(line) for line in info_lines)
    return (m.groups() for m in matches if m is not None)


def device_smart_capabilities(device):
    """Returns SMART capabilities of the given device.

    Args:
        device: (Device) Device in question.

    Returns:
        (tuple): tuple containing:

            (bool): True whenever SMART is available, False otherwise.
            (bool): True whenever SMART is enabled, False otherwise.
    """

    # NVME devices are SMART capable
    if device.type == 'nvme':
        return True, True

    groups = device_info(device)

    state = {
        g[1].split(' ', 1)[0]
        for g in groups if g[0] == 'SMART support'}

    smart_available = 'Available' in state
    smart_enabled = 'Enabled' in state

    return smart_available, smart_enabled


def collect_device_info(device):
    """Collect basic device information.

    Args:
        device: (Device) Device in question.

    Yields:
        (Metric) metrics describing general device information.
    """
    values = dict(device_info(device))
    yield Metric('device_info', {
        **device.base_labels,
        **{v: values[k] for k, v in device_info_map.items() if k in values}
    }, True)


def collect_device_health_self_assessment(device):
    """Collect metric about the device health self assessment.

    Args:
        device: (Device) Device in question.

    Yields:
        (Metric) Device health self assessment.
    """
    out = smart_ctl('--health', *device.smartctl_select(), check=False)

    self_assessment_passed = bool(self_test_re.search(out))

    yield Metric(
        'device_smart_healthy', device.base_labels, self_assessment_passed)


def collect_ata_metrics(device):
    # Fetch SMART attributes for the given device.
    attributes = smart_ctl(
        '--attributes', *device.smartctl_select()
    )

    # replace multiple occurrences of whitespace with a single whitespace
    # so that the CSV Parser recognizes individual columns properly.
    attributes = re.sub(r'[\t\x20]+', ' ', attributes)

    # Turn smartctl output into a list of lines and skip to the table of
    # SMART attributes.
    attribute_lines = attributes.strip().split('\n')[7:]

    # Some attributes have multiple IDs but have the same name.  Don't
    # yield attributes that already have been reported before.
    seen = set()

    reader = csv.DictReader(
        (line.strip() for line in attribute_lines),
        fieldnames=SmartAttribute._fields[:-1],
        restkey=SmartAttribute._fields[-1], delimiter=' ')
    for entry in reader:
        # We're only interested in the SMART attributes that are
        # whitelisted here.
        entry['name'] = entry['name'].lower()
        if entry['name'] not in smart_attributes_whitelist:
            continue

        # Ensure that only the numeric parts are fetched from the raw_value.
        # Attributes such as 194 Temperature_Celsius reported by my SSD
        # are in the format of "36 (Min/Max 24/40)" which can't be expressed
        # properly as a prometheus metric.
        m = re.match(r'^(\d+)', ' '.join(entry['raw_value']))
        if not m:
            continue
        entry['raw_value'] = m.group(1)

        # Some device models report "---" in the threshold value where most
        # devices would report "000". We do the substitution here because
        # downstream code expects values to be convertable to integer.
        if entry['threshold'] == '---':
            entry['threshold'] = '0'

        if entry['name'] in smart_attributes_whitelist and entry['name'] not in seen:
            labels = {
                'name': entry['name'],
                **device.base_labels,
            }

            for col in 'value', 'worst', 'threshold', 'raw_value':
                yield Metric(
                    'attr_{col}'.format(col=col),
                    labels, entry[col])

            seen.add(entry['name'])


def collect_ata_error_count(device):
    """Inspect the device error log and report the amount of entries.

    Args:
        device: (Device) Device in question.

    Yields:
        (Metric) Device error count.
    """
    error_log = smart_ctl(
        '-l', 'xerror,1', *device.smartctl_select(), check=False)

    m = ata_error_count_re.search(error_log)

    error_count = m.group(1) if m is not None else 0

    yield Metric('device_errors', device.base_labels, error_count)


def collect_nvme_metrics(device):
    # Fetch NVME metrics
    attributes = smart_ctl(
        '--attributes', *device.smartctl_select()
    )

    # replace multiple occurrences of whitespaces with a singel whitespace
    attributes = re.sub(r'[\t\x20]+', ' ', attributes)

    # Turn smartctl output into a list of lines and skip to the table of
    # SMART attributes.
    attribute_lines = attributes.strip().split('\n')[6:]
    for line in attribute_lines:
        label, value = line.split(':')
        if label == 'Available Spare':
            yield Metric('available_spare_ratio', device.base_labels, value[0:-1])
        elif label == 'Available Spare Threshold':
            yield Metric('available_spare_threshold_ratio', device.base_labels, value[0:-1])
        elif label == 'Percentage Used':
            yield Metric('percentage_used_ratio', device.base_labels, value[0:-1])
        elif label == 'Power Cycle':
            yield Metric('power_cycles_total', device.base_labels, value)
        elif label == 'Power On Hours':
            yield Metric('power_on_hours_total', device.base_labels, value.replace(',', ''))
        elif label == 'Temperature':
            yield Metric('temperature_celcius', device.base_labels, value.replace(' Celsius', ''))
        elif label == 'Unsafe Shutdowns':
            yield Metric('unsafe_shutdowns_total', device.base_labels, value)
        elif label == 'Media and Data Integrity Errors':
            yield Metric('media_errors_total', device.base_labels, value)
        elif label == 'Error Information Log Entries':
            yield Metric('num_err_log_entries_total', device.base_labels, value)
        elif label == 'Warning Comp. Temperature Time':
            yield Metric('warning_temperature_time_total', device.base_labels, value)
        elif label == 'Critical Comp. Temperature Time':
            yield Metric('critical_temperature_time_total', device.base_labels, value)


def collect_disks_smart_metrics(wakeup_disks):
    now = int(datetime.datetime.utcnow().timestamp())

    for device in find_devices():
        yield Metric('smartctl_run', device.base_labels, now)

        is_active = device_is_active(device)

        yield Metric('device_active', device.base_labels, is_active)

        # Skip further metrics collection to prevent the disk from
        # spinning up.
        if not is_active and not wakeup_disks:
            continue

        yield from collect_device_info(device)

        smart_available, smart_enabled = device_smart_capabilities(device)

        yield Metric(
            'device_smart_available', device.base_labels, smart_available)
        yield Metric(
            'device_smart_enabled', device.base_labels, smart_enabled)

        # Skip further metrics collection here if SMART is disabled
        # on the device.  Further smartctl invocations would fail
        # anyways.
        if not smart_available:
            continue

        yield from collect_device_health_self_assessment(device)

        if device.type.startswith('sat'):
            yield from collect_ata_metrics(device)

            yield from collect_ata_error_count(device)

        if device.type == 'nvme':
            yield from collect_nvme_metrics(device)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('-s', '--wakeup-disks', dest='wakeup_disks', action='store_true')
    args = parser.parse_args(sys.argv[1:])

    version_metric = Metric('smartctl_version', {
        'version': smart_ctl_version()
    }, True)
    metric_print_meta(version_metric, 'smartmon_')
    metric_print(version_metric, 'smartmon_')

    metrics = list(collect_disks_smart_metrics(args.wakeup_disks))
    metrics.sort(key=lambda i: i.name)

    previous_name = None
    for m in metrics:
        if m.name != previous_name:
            metric_print_meta(m, 'smartmon_')

            previous_name = m.name

        metric_print(m, 'smartmon_')


if __name__ == '__main__':
    main()

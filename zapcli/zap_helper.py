"""
Helper methods to extend and wrap the ZAP API client.

.. moduleauthor:: Daniel Grunwell (grunny)
"""

import os
import platform
import re
import shlex
import subprocess
import time
from six import binary_type

import requests
from requests.exceptions import RequestException
from zapv2 import ZAPv2

from zapcli.exceptions import ZAPError
from zapcli.log import console


class ZAPHelper(object):
    """ZAPHelper class for wrapping the ZAP API client."""

    alert_levels = {
        'Informational': 1,
        'Low': 2,
        'Medium': 3,
        'High': 4,
    }

    scanner_group_map = {
        'sqli': ['40018'],
        'xss': ['40012', '40014', '40016', '40017'],
        'xss_reflected': ['40012'],
        'xss_persistent': ['40014', '40016', '40017'],
    }

    timeout = 60
    _status_check_sleep = 10

    def __init__(self, zap_path='', port=8090, url='127.0.0.1', proxy='http://127.0.0.1', api_key='', logger=None):
        if os.path.isfile(zap_path):
            zap_path = os.path.dirname(zap_path)
        self.zap_path = zap_path
        self.port = port
        self.local_proxy = url
        self.proxy_url = '{0}:{1}'.format(proxy, self.port)
        self.zap = ZAPv2(proxies={'http': self.proxy_url, 'https': self.proxy_url}, apikey=api_key)
        self.api_key = api_key
        self.logger = logger or console

    @property
    def scanner_groups(self):
        """Available scanner groups."""
        return ['all'] + list(self.scanner_group_map.keys())

    def start(self, options=None):
        """Start the ZAP Daemon."""
        if self.is_running():
            self.logger.warn('ZAP is already running on port {0}'.format(self.port))
            return

        if platform.system() == 'Windows' or platform.system().startswith('CYGWIN'):
            executable = 'zap.bat'
        else:
            executable = 'zap.sh'

        executable_path = os.path.join(self.zap_path, executable)
        if not os.path.isfile(executable_path):
            raise ZAPError(('ZAP was not found in the path "{0}". You can set the path to where ZAP is ' +
                            'installed on your system using the --zap-path command line parameter or by ' +
                            'default using the ZAP_PATH environment variable.').format(self.zap_path))

        zap_command = [executable_path, '-daemon', '-port', str(self.port), '-host', str(self.local_proxy)]
        if options:
            extra_options = shlex.split(options)
            zap_command += extra_options

        log_path = os.path.join(self.zap_path, 'zap.log')

        self.logger.debug('Starting ZAP process with command: {0}.'.format(' '.join(zap_command)))
        self.logger.debug('Logging to {0}'.format(log_path))
        with open(log_path, 'w+') as log_file:
            subprocess.Popen(
                zap_command, cwd=self.zap_path, stdout=log_file,
                stderr=subprocess.STDOUT)

        self.wait_for_zap(self.timeout)

        self.logger.debug('ZAP started successfully.')

    def shutdown(self):
        """Shutdown ZAP."""
        if not self.is_running():
            self.logger.warn('ZAP is not running.')
            return

        self.logger.debug('Shutting down ZAP.')
        self.zap.core.shutdown(apikey=self.api_key)

        timeout_time = time.time() + self.timeout
        while self.is_running():
            if time.time() > timeout_time:
                raise ZAPError('Timed out waiting for ZAP to shutdown.')
            time.sleep(2)

        self.logger.debug('ZAP shutdown successfully.')

    def wait_for_zap(self, timeout):
        """Wait for ZAP to be ready to receive API calls."""
        timeout_time = time.time() + timeout
        while not self.is_running():
            if time.time() > timeout_time:
                raise ZAPError('Timed out waiting for ZAP to start.')
            time.sleep(2)

    def is_running(self):
        """Check if ZAP is running."""
        try:
            result = requests.get('http://'+self.local_proxy+':'+str(self.port))
        except RequestException:
            return False

        if 'ZAP-Header' in result.headers.get('Access-Control-Allow-Headers', []):
            return True

        raise ZAPError('Another process is listening on {0}'.format(self.proxy_url))

    def open_url(self, url, sleep_after_open=2):
        """Access a URL through ZAP."""
        self.zap.urlopen(url)
        # Give the sites tree a chance to get updated
        time.sleep(sleep_after_open)

    def run_spider(self, target_url, context_name=None, user_name=None):
        """Run spider against a URL."""
        self.logger.debug('Spidering target {0}...'.format(target_url))

        context_id, user_id = self._get_context_and_user_ids(context_name, user_name)

        if user_id:
            self.logger.debug('Running spider in context {0} as user {1}'.format(context_id, user_id))
            scan_id = self.zap.spider.scan_as_user(context_id, user_id, target_url, apikey=self.api_key)
        else:
            scan_id = self.zap.spider.scan(target_url, apikey=self.api_key)

        if not scan_id:
            raise ZAPError('Error running spider.')
        elif not scan_id.isdigit():
            raise ZAPError('Error running spider: "{0}"'.format(scan_id))

        self.logger.debug('Started spider with ID {0}...'.format(scan_id))

        while int(self.zap.spider.status()) < 100:
            self.logger.debug('Spider progress %: {0}'.format(self.zap.spider.status()))
            time.sleep(self._status_check_sleep)

        self.logger.debug('Spider #{0} completed'.format(scan_id))

    def run_active_scan(self, target_url, recursive=False, context_name=None, user_name=None):
        """Run an active scan against a URL."""
        self.logger.debug('Scanning target {0}...'.format(target_url))

        context_id, user_id = self._get_context_and_user_ids(context_name, user_name)

        if user_id:
            self.logger.debug('Scanning in context {0} as user {1}'.format(context_id, user_id))
            scan_id = self.zap.ascan.scan_as_user(target_url, context_id, user_id, recursive, apikey=self.api_key)
        else:
            scan_id = self.zap.ascan.scan(target_url, recurse=recursive, apikey=self.api_key)

        if not scan_id:
            raise ZAPError('Error running active scan.')
        elif not scan_id.isdigit():
            raise ZAPError(('Error running active scan: "{0}". Make sure the URL is in the site ' +
                            'tree by using the open-url or scanner commands before running an active ' +
                            'scan.').format(scan_id))

        self.logger.debug('Started scan with ID {0}...'.format(scan_id))

        while int(self.zap.ascan.status()) < 100:
            self.logger.debug('Scan progress %: {0}'.format(self.zap.ascan.status()))
            time.sleep(self._status_check_sleep)

        self.logger.debug('Scan #{0} completed'.format(scan_id))

    def run_ajax_spider(self, target_url):
        """Run AJAX Spider against a URL."""
        self.logger.debug('AJAX Spidering target {0}...'.format(target_url))

        self.zap.ajaxSpider.scan(target_url, apikey=self.api_key)

        while self.zap.ajaxSpider.status == 'running':
            self.logger.debug('AJAX Spider: {0}'.format(self.zap.ajaxSpider.status))
            time.sleep(self._status_check_sleep)

        self.logger.debug('AJAX Spider completed')

    def alerts(self, alert_level='High'):
        """Get a filtered list of alerts at the given alert level, and sorted by alert level."""
        alerts = self.zap.core.alerts()
        alert_level_value = self.alert_levels[alert_level]

        alerts = sorted((a for a in alerts if self.alert_levels[a['risk']] >= alert_level_value),
                        key=lambda k: self.alert_levels[k['risk']], reverse=True)

        return alerts

    def enabled_scanner_ids(self):
        """Retrieves a list of currently enabled scanners."""
        enabled_scanners = []
        scanners = self.zap.ascan.scanners()

        for scanner in scanners:
            if scanner['enabled'] == 'true':
                enabled_scanners.append(scanner['id'])

        return enabled_scanners

    def enable_scanners_by_ids(self, scanner_ids):
        """Enable a list of scanner IDs."""
        scanner_ids = ','.join(scanner_ids)
        self.logger.debug('Enabling scanners with IDs {0}'.format(scanner_ids))
        return self.zap.ascan.enable_scanners(scanner_ids, apikey=self.api_key)

    def disable_scanners_by_ids(self, scanner_ids):
        """Disable a list of scanner IDs."""
        scanner_ids = ','.join(scanner_ids)
        self.logger.debug('Disabling scanners with IDs {0}'.format(scanner_ids))
        return self.zap.ascan.disable_scanners(scanner_ids, apikey=self.api_key)

    def enable_scanners_by_group(self, group):
        """
        Enables the scanners in the group if it matches one in the scanner_group_map.
        """
        if group == 'all':
            self.logger.debug('Enabling all scanners')
            return self.zap.ascan.enable_all_scanners(apikey=self.api_key)

        try:
            scanner_list = self.scanner_group_map[group]
        except KeyError:
            raise ZAPError(
                'Invalid group "{0}" provided. Valid groups are: {1}'.format(
                    group, ', '.join(self.scanner_groups)
                )
            )

        self.logger.debug('Enabling scanner group {0}'.format(group))
        return self.enable_scanners_by_ids(scanner_list)

    def disable_scanners_by_group(self, group):
        """
        Disables the scanners in the group if it matches one in the scanner_group_map.
        """
        if group == 'all':
            self.logger.debug('Disabling all scanners')
            return self.zap.ascan.disable_all_scanners(apikey=self.api_key)

        try:
            scanner_list = self.scanner_group_map[group]
        except KeyError:
            raise ZAPError(
                'Invalid group "{0}" provided. Valid groups are: {1}'.format(
                    group, ', '.join(self.scanner_groups)
                )
            )

        self.logger.debug('Disabling scanner group {0}'.format(group))
        return self.disable_scanners_by_ids(scanner_list)

    def enable_scanners(self, scanners):
        """
        Enable the provided scanners by group and/or IDs.
        """
        scanner_ids = []
        for scanner in scanners:
            if scanner in self.scanner_groups:
                self.enable_scanners_by_group(scanner)
            elif scanner.isdigit():
                scanner_ids.append(scanner)
            else:
                raise ZAPError('Invalid scanner "{0}" provided. Must be a valid group or numeric ID.'.format(scanner))

        if scanner_ids:
            self.enable_scanners_by_ids(scanner_ids)

    def disable_scanners(self, scanners):
        """
        Enable the provided scanners by group and/or IDs.
        """
        scanner_ids = []
        for scanner in scanners:
            if scanner in self.scanner_groups:
                self.disable_scanners_by_group(scanner)
            elif scanner.isdigit():
                scanner_ids.append(scanner)
            else:
                raise ZAPError('Invalid scanner "{0}" provided. Must be a valid group or numeric ID.'.format(scanner))

        if scanner_ids:
            self.disable_scanners_by_ids(scanner_ids)

    def set_enabled_scanners(self, scanners):
        """
        Set only the provided scanners by group and/or IDs and disable all others.
        """
        self.logger.debug('Disabling all current scanners')
        self.zap.ascan.disable_all_scanners(apikey=self.api_key)
        self.enable_scanners(scanners)

    def set_scanner_attack_strength(self, scanner_ids, attack_strength):
        """Set the attack strength for the given scanners."""
        for scanner_id in scanner_ids:
            self.logger.debug('Setting strength for scanner {0} to {1}'.format(scanner_id, attack_strength))
            result = self.zap.ascan.set_scanner_attack_strength(scanner_id, attack_strength,
                                                                apikey=self.api_key)
            if result != 'OK':
                raise ZAPError('Error setting strength for scanner with ID {0}: {1}'.format(scanner_id, result))

    def set_scanner_alert_threshold(self, scanner_ids, alert_threshold):
        """Set the alert theshold for the given policies."""
        for scanner_id in scanner_ids:
            self.logger.debug('Setting alert threshold for scanner {0} to {1}'.format(scanner_id, alert_threshold))
            result = self.zap.ascan.set_scanner_alert_threshold(scanner_id, alert_threshold,
                                                                apikey=self.api_key)
            if result != 'OK':
                raise ZAPError('Error setting alert threshold for scanner with ID {0}: {1}'.format(scanner_id, result))

    def enable_policies_by_ids(self, policy_ids):
        """Set enabled policy from a list of IDs."""
        policy_ids = ','.join(policy_ids)
        self.logger.debug('Setting enabled policies to IDs {0}'.format(policy_ids))
        self.zap.ascan.set_enabled_policies(policy_ids, apikey=self.api_key)

    def set_policy_attack_strength(self, policy_ids, attack_strength):
        """Set the attack strength for the given policies."""
        for policy_id in policy_ids:
            self.logger.debug('Setting strength for policy {0} to {1}'.format(policy_id, attack_strength))
            result = self.zap.ascan.set_policy_attack_strength(policy_id, attack_strength,
                                                               apikey=self.api_key)
            if result != 'OK':
                raise ZAPError('Error setting strength for policy with ID {0}: {1}'.format(policy_id, result))

    def set_policy_alert_threshold(self, policy_ids, alert_threshold):
        """Set the alert theshold for the given policies."""
        for policy_id in policy_ids:
            self.logger.debug('Setting alert threshold for policy {0} to {1}'.format(policy_id, alert_threshold))
            result = self.zap.ascan.set_policy_alert_threshold(policy_id, alert_threshold,
                                                               apikey=self.api_key)
            if result != 'OK':
                raise ZAPError('Error setting alert threshold for policy with ID {0}: {1}'.format(policy_id, result))

    def exclude_from_all(self, exclude_regex):
        """Exclude a pattern from proxy, spider and active scanner."""
        try:
            re.compile(exclude_regex)
        except re.error:
            raise ZAPError('Invalid regex "{0}" provided'.format(exclude_regex))

        self.logger.debug('Excluding {0} from proxy, spider and active scanner.'.format(exclude_regex))

        self.zap.core.exclude_from_proxy(exclude_regex, apikey=self.api_key)
        self.zap.spider.exclude_from_scan(exclude_regex, apikey=self.api_key)
        self.zap.ascan.exclude_from_scan(exclude_regex, apikey=self.api_key)

    def new_session(self):
        """Start a new session."""
        self.logger.debug('Starting a new session')
        self.zap.core.new_session(apikey=self.api_key)

    def save_session(self, file_path):
        """Save the current session."""
        self.logger.debug('Saving the session to "{0}"'.format(file_path))
        self.zap.core.save_session(file_path, overwrite='true', apikey=self.api_key)

    def load_session(self, file_path):
        """Load a given session."""
        if not os.path.isfile(file_path):
            raise ZAPError('No file found at "{0}", cannot load session.'.format(file_path))
        self.logger.debug('Loading session from "{0}"'.format(file_path))
        self.zap.core.load_session(file_path, apikey=self.api_key)

    def is_valid_script_engine(self, engine):
        """Check if given script engine is valid."""
        engine_names = self.zap.script.list_engines
        short_names = [e.split(' : ')[1] for e in engine_names]

        return engine in engine_names or engine in short_names

    def enable_script(self, script_name):
        """Enable a given script."""
        self.logger.debug('Enabling script "{0}"'.format(script_name))
        result = self.zap.script.enable(script_name, apikey=self.api_key)

        if result != 'OK':
            raise ZAPError('Error enabling script: {0}'.format(result))

    def disable_script(self, script_name):
        """Disable a given script."""
        self.logger.debug('Disabling script "{0}"'.format(script_name))
        result = self.zap.script.disable(script_name, apikey=self.api_key)

        if result != 'OK':
            raise ZAPError('Error disabling script: {0}'.format(result))

    def remove_script(self, script_name):
        """Remove a given script."""
        self.logger.debug('Removing script "{0}"'.format(script_name))
        result = self.zap.script.remove(script_name, apikey=self.api_key)

        if result != 'OK':
            raise ZAPError('Error removing script: {0}'.format(result))

    def load_script(self, name, script_type, engine, file_path, description=''):
        """Load a given script."""
        if not os.path.isfile(file_path):
            raise ZAPError('No file found at "{0}", cannot load script.'.format(file_path))

        if not self.is_valid_script_engine(engine):
            engines = self.zap.script.list_engines
            raise ZAPError('Invalid script engine provided. Valid engines are: {0}'.format(', '.join(engines)))

        self.logger.debug('Loading script "{0}" from "{1}"'.format(name, file_path))
        result = self.zap.script.load(name, script_type, engine, file_path,
                                      scriptdescription=description, apikey=self.api_key)

        if result != 'OK':
            raise ZAPError('Error loading script: {0}'.format(result))

    def xml_report(self, file_path):
        """Generate and save XML report"""
        self.logger.debug('Generating XML report')
        report = self.zap.core.xmlreport(apikey=self.api_key)
        self._write_report(report, file_path)

    def md_report(self, file_path):
        """Generate and save MD report"""
        self.logger.debug('Generating MD report')
        report = self.zap.core.mdreport(apikey=self.api_key)
        self._write_report(report, file_path)

    def html_report(self, file_path):
        """Generate and save HTML report."""
        self.logger.debug('Generating HTML report')
        report = self.zap.core.htmlreport(apikey=self.api_key)
        self._write_report(report, file_path)

    @staticmethod
    def _write_report(report, file_path):
        """Write report to the given file path."""
        with open(file_path, mode='wb') as f:
            if not isinstance(report, binary_type):
                report = report.encode('utf-8')
            f.write(report)

    def new_context(self, context_name):
        """Create a new context with the given name."""
        return self.zap.context.new_context(contextname=context_name, apikey=self.api_key)

    def include_in_context(self, context_name, regex):
        """Add include regex to context."""
        try:
            re.compile(regex)
        except re.error:
            raise ZAPError('Invalid regex "{0}" provided'.format(regex))

        result = self.zap.context.include_in_context(contextname=context_name, regex=regex, apikey=self.api_key)

        if result != 'OK':
            raise ZAPError('Including regex from context failed: {}'.format(result))

    def exclude_from_context(self, context_name, regex):
        """Add exclude regex to context."""
        try:
            re.compile(regex)
        except re.error:
            raise ZAPError('Invalid regex "{0}" provided'.format(regex))

        result = self.zap.context.exclude_from_context(contextname=context_name, regex=regex, apikey=self.api_key)

        if result != 'OK':
            raise ZAPError('Excluding regex from context failed: {}'.format(result))

    def get_context_info(self, context_name):
        """Get the context ID for a given context name."""
        context_info = self.zap.context.context(context_name)
        if not isinstance(context_info, dict):
            raise ZAPError('Context with name "{0}" wasn\'t found'.format(context_name))

        return context_info

    def import_context(self, file_path):
        """Import a context from a file."""
        result = self.zap.context.import_context(file_path, apikey=self.api_key)

        if not result.isdigit():
            raise ZAPError('Importing context from file failed: {}'.format(result))

    def export_context(self, context_name, file_path):
        """Export a given context to a file."""
        result = self.zap.context.export_context(context_name, file_path, apikey=self.api_key)

        if result != 'OK':
            raise ZAPError('Exporting context to file failed: {}'.format(result))

    def _get_context_and_user_ids(self, context_name, user_name):
        """Helper to get the context ID and user ID from the given names."""
        if context_name is None:
            return None, None

        context_id = self.get_context_info(context_name)['id']
        user_id = None
        if user_name:
            user_id = self._get_user_id_from_name(context_id, user_name)

        return context_id, user_id

    def _get_user_id_from_name(self, context_id, user_name):
        """Get a user ID from the user name."""
        users = self.zap.users.users_list(context_id)
        for user in users:
            if user['name'] == user_name:
                return user['id']

        raise ZAPError('No user with the name "{0}"" was found for context {1}'.format(user_name, context_id))

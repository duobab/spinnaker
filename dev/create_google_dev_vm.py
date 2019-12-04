#!/usr/bin/python
#
# Copyright 2015 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
import os
import re
import sys
import tempfile
import time

try:
  from urllib2 import urlopen, Request, HTTPError, URLError
except ImportError:
  from urllib.request import urlopen, Request
  from urllib.error import HTTPError, URLError

from buildtool import (
    run_subprocess,
    check_subprocess)


GOOGLE_METADATA_URL = 'http://metadata.google.internal/computeMetadata/v1'
GOOGLE_INSTANCE_METADATA_URL = GOOGLE_METADATA_URL + '/instance'


__NEXT_STEP_INSTRUCTIONS = """
To finish the installation, follow these steps:

(1) Log into your new instance (with or without tunneling ssh-flags):

  gcloud compute ssh --project {project} --zone {zone} {instance}\
 --ssh-flag="-L 9000:localhost:9000"\
 --ssh-flag="-L 8084:localhost:8084"


(2) Wait for the installation to complete:

  tail -f /var/log/syslog

  When the instance startup script finishes installing the developer tools
  you will be ready to continue. ^C to terminate the tail process.


(3) Set up the build environment:

  source /opt/spinnaker/install/bootstrap_dev.sh


For more information about Spinnaker, see http://spinnaker.io

"""


def get_project(options):
    """Determine the default project name.

    The default project name is the gcloud configured default project.
    """
    if not options.project:
      stdout = check_subprocess('gcloud config list')
      options.project = re.search('project = (.*)\n', stdout).group(1)
    return options.project


def get_zone(options):
    """Determine the default availability zone.

    The default zone is the current zone if on GCE or an arbitrary zone.
    """
    if not options.zone:
      request = Request(GOOGLE_INSTANCE_METADATA_URL + '/zone')
      request.add_header('Metadata-Flavor', 'Google')
      try:
        response = urlopen(request)
        options.zone = os.path.basename(bytes.decode(response.read()))
      except (HTTPError, URLError):
        options.zone = 'us-central1-f'

    return options.zone


def init_argument_parser(parser):
    parser.add_argument(
        '--instance',
        default='{user}-spinnaker-dev'.format(user=os.environ['USER']),
        help='The name of the GCE instance to create.')
    parser.add_argument(
        '--project', default=None,
        help='The Google Project ID to create the new instance in.'
        ' If left empty, use the default project gcloud was configured with.')

    parser.add_argument(
        '--zone', default=None,
        help='The Google Cloud Platform zone to create the new instance in.')

    parser.add_argument(
        '--disk_type',  default='pd-ssd',
        help='The Google Cloud Platform disk type to use for the new instance.'
        '  The default is pd-standard. For a list of other available options,'
        ' see "gcloud compute disk-types list".')
    parser.add_argument('--disk_size', default='200GB',
                        help='Warnings appear if disk size < 200GB')
    parser.add_argument('--machine_type', default='n1-standard-8')

    parser.add_argument(
        '--copy_personal_files', default=True, action='store_true',
        help='Copy personal configuration files (.gitconfig, etc.)')
    parser.add_argument(
        '--no_personal_files', dest='copy_personal_files',
        action='store_false', help='Do not copy personal files.')

    parser.add_argument(
        '--copy_home_spinnaker', default=False, action='store_true',
      help='Copy ~/.spinnaker directory.')
    parser.add_argument(
        '--copy_git_credentials', default=False, action='store_true',
        help='Copy git credentials (.git-credentials)')
    parser.add_argument(
        '--no_git_credentials', dest='copy_git_credentials',
        action='store_false', help='Do not copy git credentials')

    parser.add_argument(
        '--copy_gcloud_config', default=False, action='store_true',
        help='Copy private gcloud configuration files.')
    parser.add_argument(
        '--no_gcloud_config', dest='copy_gcloud_config',
        action='store_false',
        help='Do not copy private gcloud configuration files.')

    parser.add_argument(
        '--aws_credentials', default=None,
        help='If specified, the path to the aws credentials file.')

    parser.add_argument(
        '--address', default=None,
        help='The IP address to assign to the new instance. The address may'
             ' be an IP address or the name or URI of an address resource.')
    parser.add_argument(
        '--scopes', default='compute-rw,storage-full,monitoring-write,logging-write',
        help='Create the instance with these scopes.'
        'The default are the minimal scopes needed to run the development'
        ' scripts. This is currently "compute-rw,storage-rw,monitoring-write,logging-write".')


def try_until_ready(command):
    while True:
        retcode, stdout = run_subprocess(command)
        if not retcode:
            break

        if stdout.find('refused') > 0:
            print('New instance does not seem ready yet...retry in 5s.')
        else:
            print(stdout.strip())
            print('Retrying in 5s.')
        time.sleep(5)


def make_remote_directories(options):
    all = []
    if options.copy_home_spinnaker:
        all.append('.hal')
        all.append('.spinnaker')
    if options.copy_personal_files:
        all.append('.gradle')
    if options.aws_credentials:
        all.append('.aws')
    if options.copy_gcloud_config:
        all.append('.config/gcloud')

    if all:
        command = ' '.join([
            'gcloud compute ssh',
            options.instance,
            '--project', get_project(options),
            '--zone', get_zone(options),
            '--command=\'bash -c "for i in {0}; do mkdir -p \\$i; done"\''.format(' '.join(all))])

        try_until_ready(command)

def copy_dir(options, source, target):
    print('Copying dir %s to %s' % (source, target))
    command = ' '.join([
        'gcloud compute scp',
        '--project', get_project(options),
        '--zone', get_zone(options),
        '--recurse',
        source,
        '{instance}:{target}'.format(instance=options.instance,
                                     target=target)])
    try_until_ready(command)

def copy_custom_file(options, source, target):
    command = ' '.join([
        'gcloud compute copy-files',
        '--project', get_project(options),
        '--zone', get_zone(options),
        source,
        '{instance}:{target}'.format(instance=options.instance,
                                     target=target)])
    try_until_ready(command)


def copy_home_file_list(options, type, base_dir, sources):
    have = []
    for file in sources:
        full_path = os.path.abspath(
            os.path.join(os.environ['HOME'], base_dir, file))
        if os.path.exists(full_path):
            have.append('"{0}"'.format(full_path))

    if have:
        print('Copying {type}...'.format(type=type))
        source_list = ' '.join(have)
        # gcloud will copy permissions as well, however it won't create
        # directories. Assume make_remote_directories was called already.
        copy_custom_file(options, source_list, base_dir)
    else:
        print('Skipping {type} because there are no files.'.format(type=type))


def maybe_inform(type, test_path, option_to_enable):
    if os.path.exists(os.path.join(os.environ['HOME'], test_path)):
        print('Skipping {type} because missing {option}.'.format(
            type=type, option=option_to_enable))


def maybe_copy_aws_credentials(options):
    if options.aws_credentials:
      print('Copying aws credentials...')
      copy_custom_file(options, options.aws_credentials, '.aws/credentials')
    else:
      maybe_inform('aws credentials', '.aws/credentials', '--aws_credentials')


def maybe_copy_gcloud_config(options):
   if options.copy_gcloud_config:
       copy_home_file_list(options,
                           'gcloud credentials',
                           '.config/gcloud',
                           ['application_default_credentials.json',
                            'credentials',
                            'properties'])
   else:
      maybe_inform('gcloud credentials',
                   '.config/gcloud/credentials', '--copy_gcloud_config')


def maybe_copy_git_credentials(options):
    if options.copy_git_credentials:
        copy_home_file_list(options, 'git credentials',
                            '.', ['.git-credentials'])
    else:
        maybe_inform('git credentials',
                     '.git-credentials', '--copy_git_credentials')


def maybe_copy_home_spinnaker(options):
  if not options.copy_home_spinnaker:
    return

  home_path = os.environ['HOME']
  copy_dir(options, os.path.join(home_path, '.spinnaker'), '.')
  copy_dir(options, os.path.join(home_path, '.hal'), '.')


def copy_personal_files(options):
   copy_home_file_list(options, 'personal configuration files',
                       '.',
                       ['.gitconfig', '.emacs', '.bashrc', '.screenrc'])
   # Ideally this is part of the above, but it goes into a different directory.
   copy_home_file_list(options, 'gradle configuration',
                       '.gradle', ['gradle.properties'])


def create_instance(options):
    """Creates new GCE VM instance for development."""
    project = get_project(options)
    print('Creating instance {project}/{zone}/{instance}'.format(
        project=project, zone=get_zone(options), instance=options.instance))
    print('  with --machine_type={type} and --disk_size={disk_size}...'
          .format(type=options.machine_type, disk_size=options.disk_size))

    google_dev_dir = os.path.join(os.path.dirname(__file__), '../google/dev')
    dev_dir = os.path.dirname(__file__)
    project_dir = os.path.join(dev_dir, '..')

    install_dir = '{dir}/../install'.format(dir=dev_dir)

    startup_command = ['/opt/spinnaker/install/install_development.sh']
    fd, temp_startup = tempfile.mkstemp()
    os.write(fd, str.encode(';'.join(startup_command)))
    os.close(fd)

    metadata_files = [
        'startup-script={google_dev_dir}/google_install_loader.py'
        ',sh_bootstrap_dev={dev_dir}/bootstrap_dev.sh'
        ',sh_install_development={dev_dir}/install_development.sh'
        ',startup_command={temp_startup}'
        .format(google_dev_dir=google_dev_dir,
                dev_dir=dev_dir,
                project_dir=project_dir,
                temp_startup=temp_startup)]

    metadata = ','.join([
        'startup_loader_files='
        'sh_install_development'
        '+sh_bootstrap_dev'])

    command = ['gcloud', 'compute', 'instances', 'create',
               options.instance,
               '--project', get_project(options),
               '--zone', get_zone(options),
               '--machine-type', options.machine_type,
               '--image-family', 'ubuntu-1604-lts',
               '--image-project', 'ubuntu-os-cloud',
               '--scopes', options.scopes,
               '--boot-disk-size={size}'.format(size=options.disk_size),
               '--boot-disk-type={type}'.format(type=options.disk_type),
               '--metadata', metadata,
               '--metadata-from-file={files}'.format(
                   files=','.join(metadata_files))]
    if options.address:
        command.extend(['--address', options.address])

    check_subprocess(' '.join(command))


def check_gcloud():
    retcode, _ = run_subprocess('gcloud --version')
    if not retcode:
        return

    sys.stderr.write('ERROR: This program requires gcloud. To obtain gcloud:\n'
                     '       curl https://sdk.cloud.google.com | bash\n')
    sys.exit(-1)


def check_args(options):
    """Fail fast if paths we explicitly want to copy do not exist."""
    for path in [options.aws_credentials]:
        if path and not os.path.exists(path):
           sys.stderr.write('ERROR: {path} not found.\n'.format(path=path))
           sys.exit(-1)


if __name__ == '__main__':
    check_gcloud()

    parser = argparse.ArgumentParser()
    init_argument_parser(parser)
    options = parser.parse_args()

    check_args(options)
    create_instance(options)


    make_remote_directories(options)
    if options.copy_personal_files:
      copy_personal_files(options)

    maybe_copy_git_credentials(options)
    maybe_copy_aws_credentials(options)
    maybe_copy_home_spinnaker(options)
    maybe_copy_gcloud_config(options)

    print(__NEXT_STEP_INSTRUCTIONS.format(
        project=get_project(options),
        zone=get_zone(options),
        instance=options.instance))

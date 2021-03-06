"""
    Copyright (C) 2017, ContraxSuite, LLC

    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU Affero General Public License as
    published by the Free Software Foundation, either version 3 of the
    License, or (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU Affero General Public License for more details.

    You should have received a copy of the GNU Affero General Public License
    along with this program.  If not, see <http://www.gnu.org/licenses/>.

    You can also be released from the requirements of the license by purchasing
    a commercial license from ContraxSuite, LLC. Buying such a license is
    mandatory as soon as you develop commercial activities involving ContraxSuite
    software without disclosing the source code of your own applications.  These
    activities include: offering paid services to customers as an ASP or "cloud"
    provider, processing documents on the fly in a web application,
    or shipping ContraxSuite within a closed source product.
"""
# -*- coding: utf-8 -*-
# Standard imports
import configparser
import csv
import datetime
import os
import re
import sys
from collections import OrderedDict
from contextlib import contextmanager
from functools import wraps

# Fabric imports
from fabric.api import env, prefix
from fabric.colors import red, green, blue, yellow
from fabric.decorators import task
from fabric.operations import get, hide, local as _local, \
    run as _run, sudo as _sudo, reboot, put
from fabric.context_managers import cd, settings
from fabric.contrib import django
from fabric.contrib.files import exists, upload_template
from fabtools.postgres import (create_database,
                               create_user as create_pg_user,
                               database_exists,
                               user_exists as pg_user_exists)

__author__ = "ContraxSuite, LLC; LexPredict, LLC"
__copyright__ = "Copyright 2015-2018, ContraxSuite, LLC"
__license__ = "https://github.com/LexPredict/lexpredict-contraxsuite/blob/1.0.8/LICENSE"
__version__ = "1.0.9"
__maintainer__ = "LexPredict, LLC"
__email__ = "support@contraxsuite.com"


"""
Update env from base/fabricrc
"""

try:
    with open('base/fabricrc', 'r') as f:
        config_string = '[dummy_section]\n' + f.read()
    config = configparser.ConfigParser()
    config.read_string(config_string)
    for key, val in config.items('dummy_section'):
        if key in env:
            continue
        env[key] = val
except FileNotFoundError:
    pass

"""
Fabric setup for executing host.
"""

USER_HOME = os.path.expanduser('~')

# Determine base configuration directory; based on fabricrc path in env
env.config_dir = os.path.dirname(os.path.abspath(env.rcfile))
env.base_config_dir = os.path.join(os.path.dirname(__file__), 'base')

if 'localhost' not in env.hosts:

    # Check env.key_filename.
    if not env.key_filename:
        raise RuntimeError('No env.key_filename set; ' +
                           'are you sure you passed -c fabric?')

    ssh_key_locations = (
        os.path.join(USER_HOME, '.ssh'),
        os.path.dirname(__file__),
        env.config_dir,
        env.base_config_dir)

    key_location = None
    for ssh_dir in ssh_key_locations:
        location = os.path.join(ssh_dir, env.key_filename)
        if os.path.exists(location):
            env.key_filename = key_location = location
            break
    if key_location is None:
        raise RuntimeError('Unable to locate SSH key file ' +
                           'from key_filename value "{}"'.format(env.key_filename))

REBOOT_TIME = 300

# Path configuration parameters
env.project_dir = os.path.join(env.base_dir, env.project_path)
env.virtualenv_dir = os.path.join(env.base_dir, env.ve_dir)
env.ve_bin = os.path.join(env.virtualenv_dir, 'bin')
env.python_bin = os.path.join(env.ve_bin, 'python')
env.pip_bin = os.path.join(env.ve_bin, 'pip')
env.uwsgi_bin = os.path.join(env.ve_bin, 'uwsgi')
env.manage_py = os.path.join(env.project_dir, 'manage.py')
env.uwsgi_name = '%s_uwsgi' % env.templates_prefix

"""
Get local django settings
"""

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
try:
    django.settings_module('settings')
    from django.conf import settings as dj_settings
    STATICFILES_DIR = dj_settings.STATICFILES_DIRS[0].replace(
        dj_settings.PROJECT_DIR.root, env.project_dir)
    STATIC_ROOT = dj_settings.STATIC_ROOT.replace(dj_settings.PROJECT_DIR.root, env.project_dir)
    MEDIA_ROOT = dj_settings.MEDIA_ROOT.replace(dj_settings.PROJECT_DIR.root, env.project_dir)
    FILEBROWSER_DIRECTORY = dj_settings.FILEBROWSER_DIRECTORY
    CELERY_LOG_FILE_PATH = dj_settings.CELERY_LOG_FILE_PATH
    LOG_FILE_PATH = dj_settings.LOG_FILE_PATH
    DB_LOG_FILE_PATH = dj_settings.DB_LOG_FILE_PATH
except (AttributeError, ImportError):
    STATICFILES_DIR = os.path.join(env.project_dir, '..', 'static')
    STATIC_ROOT = os.path.join(env.project_dir, 'staticfiles')
    MEDIA_ROOT = os.path.join(env.project_dir, 'media')
    FILEBROWSER_DIRECTORY = 'data/documents/'
    LOG_FILE_PATH = 'logs/log.txt'
    CELERY_LOG_FILE_PATH = 'logs/celery.log'
    DB_LOG_FILE_PATH = 'logs/db.log'


templates = OrderedDict((
    ('run', {
        'local_path': 'templates/run.sh',
        'remote_path': '~/run.sh'
    }),
    ('502', {
        'local_path': 'templates/502.html',
        'remote_path': '/usr/share/nginx/html/502.html',
        'use_jinja': 'true'
    }),
    ('uwsgi-init', {
        'local_path': 'templates/uwsgi.service',
        'remote_path': '/etc/systemd/system/%s.service' % env.uwsgi_name
    }),
    ('uwsgi', {
        'local_path': 'templates/uwsgi.ini',
        'remote_path': '/etc/uwsgi/%s.ini' % env.uwsgi_name
    }),
    ('settings', {
        'template_dir': '%(config_dir)s',
        'local_path': 'local_settings.py',
        'remote_path': '%(project_dir)s/local_settings.py'
    }),
    ('nginx', {
        'local_path': 'templates/nginx.conf',
        'remote_path': '/etc/nginx/sites-enabled/%s_nginx.conf' % env.templates_prefix,
        'reload_command': 'systemctl restart nginx',
        'use_jinja': 'true',
    }),
))

"""
--------------------------------
Print methods
--------------------------------
"""


def _print(output):
    print()
    print(output)


def print_command(command):
    _print(blue("$ ", bold=True) +
           yellow(command, bold=True) +
           red(" ->", bold=True))


def log_call(func):
    @wraps(func)
    def logged(*args, **kawrgs):
        header = "-" * len(func.__name__)
        _print(green("\n".join([header, func.__name__, header]), bold=True))
        return func(*args, **kawrgs)

    return logged


"""
--------------------------------
Installers methods
--------------------------------
"""


def get_templates():
    """
    Returns each of the templates with env vars injected.
    """
    injected = {}
    for template_name, data in templates.items():
        injected[template_name] = dict([(k, v % env) for k, v in data.items()])
    return injected


@task
def upload_template_and_reload(template_name):
    """
    Uploads a template only if it has changed, and if so, reload a related service.
    """
    template = get_templates()[template_name]
    local_path = template['local_path']
    remote_path = template['remote_path']
    reload_command = template.get('reload_command')
    owner = template.get('owner')
    mode = template.get('mode')
    template_dir = template.get('template_dir', '.')
    upload_template(local_path, remote_path, env, use_sudo=True, backup=False,
                    template_dir=template_dir, use_jinja=template.get('use_jinja'))
    if owner:
        sudo('chown %s %s' % (owner, remote_path))
    if mode:
        sudo('chmod %s %s' % (mode, remote_path))
    if reload_command:
        sudo(reload_command)


@task
@log_call
def upload_templates(template_names=None):
    """
    Upload given templates
    """
    for template_name in template_names or templates:
        upload_template_and_reload(template_name)
    sudo('systemctl daemon-reload')


@task
def install_packages(install_command,
                     requirements_filename,
                     package_list=None,
                     installed_packages=None,
                     use_sudo=False):
    """
    Install packages from custom files from given custom and base config dirs.
    """
    run_ = sudo if use_sudo else run
    req_paths = ((env.base_config_dir, requirements_filename),
                 (env.config_dir, requirements_filename))
    if package_list is None:
        package_list = []
    for path_terms in req_paths:
        path = os.path.join(*path_terms)
        if not os.path.exists(path):
            continue
        csv_file = open(path)
        csv_reader = csv.reader(csv_file)
        for tokens in csv_reader:
            if not tokens:
                continue
            package = tokens[0]
            if package.strip().startswith('#'):
                continue
            if installed_packages and package in installed_packages:
                print('Package "{}" already exists'.format(package))
                continue
            package_list.append(package)
        csv_file.close()

        for package in package_list:
            apt_ret = run_('{} {}'.format(install_command, package))
            if apt_ret.failed:
                raise RuntimeError('Unable to install package {}.'.format(package))


@task
@log_call
def python_install(upgrade=False):
    """
    Install required python packages.
    """
    with virtualenv():
        installed_packages = run('pip freeze').split()
        install_command = 'pip install {}'.format('-U' if upgrade else '')
        install_packages(install_command,
                         'python-requirements.txt',
                         installed_packages=installed_packages)


@task
def git_clone(recreate=True):
    """
    Run initial `git clone` into BASE_DIR.
    """
    with cd(env.base_dir):
        # Check for existing git directory. If exists and recreate, delete.
        if exists(env.project_dir):
            if not recreate:
                git_pull()
            else:
                # Backup the folder
                date_string = datetime.datetime.now().strftime('%Y%m%d%_H%M%S')
                repo_original_path = os.path.normpath(os.path.join(env.project_dir, '..'))
                repo_backup_path = '{}.{}'.format(repo_original_path, date_string)
                run_check('mv {} {}'.format(repo_original_path, repo_backup_path))
        # Clone
        result = run_check('git clone --branch {} {}'.format(env.git_branch, env.git_uri))
    return result


@task
@log_call
def git_pull(branch=None):
    """
    Update git by pulling.
    """
    if not branch:
        branch = env.git_branch
    with cd(env.project_dir):
        run_check('git fetch')
        run_check('git checkout {}'.format(branch))
        run_check('git pull origin {}'.format(branch))
        run_check('find . -name "*pyc" -delete', use_sudo=True)


@task
@log_call
def git_status(branch=None):
    """
    Update git by pulling.
    """
    if not branch:
        branch = env.git_branch
    with cd(env.project_dir):
        run_check('git checkout {}'.format(branch))
        run_check('git status')


"""
--------------------------------
Install instance
--------------------------------
"""

"""
1. create_ssh_keys
2. add id_rsa.pub file content into github account
3. should have assigned dns name instead of IP in fabricrc
   for proper ssl certification
4. setup_new_app_instance:1 if ssh keys added and dns name exists
   otherwise setup_new_app_instance and partially install_project_files
"""


@task
def create_ssh_keys():
    """
    Create id-rsa key. Don't forget to add it to GIT
    """
    run_check('echo -e \'y\n\'|ssh-keygen -q -t rsa -N "" -f ~/.ssh/id_rsa')
    run_check('chmod 600 ~/.ssh/id_rsa')
    run_check('eval "$(ssh-agent -s)"')
    run_check('ssh-add ~/.ssh/id_rsa')
    run_check('cat ~/.ssh/id_rsa.pub')


@task
def setup_new_app_instance(install_project=False):
    """
    Setup a new app instance from base Ubuntu image
    """
    debian_install()
    locales_install()
    postgres_create()
    init_daemon_install()
    debian_upgrade_reboot()
    create_base_directory()
    python_install()
    # RabbitMQ is used as message broker.
    rabbitmq_install()
    # Installing redis to allow easy switching and for possible usage as key-value storage.
    redis_install()
    java_install()
    elasticsearch_install()
    theme_install()
    jqwidgets_install()
    # stanford_install()

    if install_project:
        install_project_files()


@task
def install_project_files():
    git_clone()
    create_dirs()
    upload_templates(['nginx', 'uwsgi-init', 'uwsgi',
                      'settings', 'run', '502'])

    # run migrations without Django's system check
    manage('force_migrate')
    # manage('migrate --noinput')

    # create superuser
    create_superuser()

    # setup site object
    manage('set_site')

    # collect static
    manage('collectstatic -v 0 --noinput')

    # download nltk data
    nltk_download()

    ssl_install()

    start()


@task
def create_dirs():
    """
    Create directories and files for the project and its services.
    """
    # remove default nginx config
    sudo('rm -f /etc/nginx/sites-enabled/default')
    # create static and media dirs
    mkdir(STATIC_ROOT, env.user, env.user, True)
    mkdir(MEDIA_ROOT, env.user, env.user, True)
    # create dirs for documents
    mkdir(os.path.join(MEDIA_ROOT, FILEBROWSER_DIRECTORY), env.user, env.user, True)
    # create tika log file, otherwise celery won't register tasks
    run_check('touch /tmp/tika.log')
    sudo('chown -R {}:{} /tmp/tika.log'.format(env.user, env.user))
    # create log files
    logs_dir_path = os.path.join(env.project_dir, 'logs')
    mkdir(logs_dir_path, env.user, env.user, True)
    for log_path in (LOG_FILE_PATH, CELERY_LOG_FILE_PATH, DB_LOG_FILE_PATH):
        #log_path = os.path.join(logs_dir_path, log_path)
        sudo('touch %s' % log_path)
        sudo('chown -R {}:{} {}'.format(env.user, env.user, log_path))


@task
def create_base_directory(clean=False):
    """
    Create base directory.
    """
    # Check if we want to clean.
    if clean and exists(env.base_dir):
        clean_base_directory()

    # Create path
    mkdir(env.base_dir, env.user, env.user, True)

    # Create Python virtualenv
    run('virtualenv -p python3 {}'.format(env.virtualenv_dir))

    # Check that Python and pip executable exist.
    if not exists(env.python_bin):
        raise RuntimeError('PYTHON_BIN {} does not exist; setup failed.'.format(env.python_bin))

    if not exists(env.pip_bin):
        raise RuntimeError('PIP_BIN {} does not exist; setup failed.'.format(env.pip_bin))


"""
--------------------------------
Services methods
--------------------------------
"""


@task
def status_service(service_name):
    """
    Get status of systemd service
    """
    sudo('systemctl status %s --no-pager -l' % service_name, warn_only=True)


@task
def is_active(service_name):
    """
    Check if service is active
    """
    ret = sudo('systemctl is-active %s' % service_name, warn_only=True)
    active = ret == 'active'
    color = green if active else red
    print(color('Status %s: %s' % (service_name, ret)))
    return active


@task
def restart_service(service_name):
    """
    Restart service
    """
    cmd = 'restart' if is_active(service_name) else 'start'
    sudo('systemctl %s %s' % (cmd, service_name))


@task
def stop_service(service_name):
    """
    Stop service
    """
    if is_active(service_name):
        sudo('systemctl stop %s' % service_name)


@task
def start_service(service_name):
    """
    Start service
    """
    if not is_active(service_name):
        sudo('systemctl start %s' % service_name)


@task
def stop_celery(kill_process=False):
    """
    Stop celery workers
    """
    with cd(env.project_dir):
        if kill_process:
            run('pkill -f "celery worker .*-A apps"')
        else:
            run('{ve_dir}/bin/celery multi stop {celery_worker} -A {celery_app}'.format(
                ve_dir=env.virtualenv_dir,
                celery_worker=env.celery_worker,
                celery_app=env.celery_app))


@task
def start_celery():
    """
    Start celery workers
    """
    with cd(env.project_dir):
        run('{run_as_root}{ve_dir}/bin/celery multi start '
            '{celery_worker} -A {celery_app} -f {log_file_path} {opts}'.format(
            run_as_root='C_FORCE_ROOT ' if env.celery_run_as_root == 'true' else '',
            ve_dir=env.virtualenv_dir,
            celery_worker=env.celery_worker,
            celery_app=env.celery_app,
            opts=env.celery_opts,
            log_file_path=CELERY_LOG_FILE_PATH))


@task
def status_celery():
    """
    Show celery registered and active tasks
    """
    with cd(env.project_dir):
        run('{ve_dir}/bin/celery -A {celery_app} inspect registered'.format(
            ve_dir=env.virtualenv_dir,
            celery_app=env.celery_app))
        run('{ve_dir}/bin/celery -A {celery_app} inspect active'.format(
            ve_dir=env.virtualenv_dir,
            celery_app=env.celery_app))


@task
def purge_celery():
    """
    Purge celery tasks
    """
    with cd(env.project_dir):
        run('{ve_dir}/bin/celery -A {celery_app} purge -f'.format(
            ve_dir=env.virtualenv_dir,
            celery_app=env.celery_app))


@task
def stop_redis():
    stop_service('redis_6379')


@task
def start_redis():
    start_service('redis_6379')


@task
@log_call
def stop():
    """
    Stop services
    """
    stop_service('nginx')
    stop_service(env.uwsgi_name)
    stop_celery()
    # stop_redis()


@task
@log_call
def start():
    """
    Start services
    """
    start_service('nginx')
    start_service(env.uwsgi_name)
    start_celery()
    # redis doesn't start properly
    # start_redis()


@task
@log_call
def restart():
    """
    Restart services
    """
    stop()
    start()


"""
--------------------------------
Deploy methods
--------------------------------
"""


@task
@log_call
def deploy(do_upload_templates=False):
    """
    Refresh a site by pulling latest repository changes,
    deploying newest configuration templates,
    and restarting services.
    """

    # Stop services
    stop()

    # remove *pyc files
    run_check('find {} -name "*pyc" -delete'.format(env.project_dir), use_sudo=True)

    # upload config. files
    if do_upload_templates:
        upload_templates(['nginx', 'uwsgi-init', 'uwsgi', 'settings'])

    # Git pull
    git_pull()
    python_install()

    # run migrations
    manage('migrate --noinput')

    # Start services
    start()

    manage('collectstatic -v 0 --noinput')


@task
@log_call
def deploy1():
    """
    Short deploy variant
    """
    git_pull()
    restart()


"""
--------------------------------
Fabric system utils
--------------------------------
"""


@task
def manage(cmd):
    """
    Run django management command
    """
    with cd(env.project_dir):
        sudo('{} manage.py {}'.format(env.python_bin, cmd))


def run_check(command, use_sudo=False, combine_stderr=True, **kw):
    """
    Wrapper around run/sudo that checks for error code/value.
    """
    with settings(warn_only=True):
        if use_sudo:
            ret = sudo(command, combine_stderr=combine_stderr, **kw)
        else:
            ret = run(command, combine_stderr=combine_stderr, **kw)
    if ret.failed:
        raise RuntimeError('Fail in command: %s . Exit code:  %s' % (
            command, ret.return_code))
    return ret


def mkdir(path, owner=env.user, group=env.user, use_sudo=False):
    """
    Create a path with a given owner/group, possibly via sudo.
    """
    if use_sudo:
        sudo('mkdir -p {}'.format(path))
    else:
        run('mkdir -p {}'.format(path))

    sudo('chown -R {}:{} {}'.format(owner, group, path))


"""
--------------------------------
Install methods
--------------------------------
"""


def debian_add_key(keyserver, recv):
    """
    Add an apt key.
    """
    # Use apt-key to add this
    run_check('apt-key adv --keyserver {} --recv {}'.format(keyserver, recv), use_sudo=True)


def debian_add_repository(repository_string):
    """
    Add an apt repository.
    """
    # Use apt-add-repository
    run_check("apt-add-repository '{}'".format(repository_string), use_sudo=True)


@task
def debian_update(fix_missing=False):
    """
    Refresh apt repository cache.
    """
    # Update repo cache
    if fix_missing:
        update_ret = sudo('apt-get -y update --fix-missing')
    else:
        update_ret = sudo('apt-get -y update')
    if update_ret.failed:
        raise RuntimeError('Unable to update apt repository information.')


@task
@log_call
def debian_install(package_list=None, update_cache=True):
    """
    Install required debian/ubuntu packages.
    If no package list specified, assume from config;
    iterate over all lines of debian-requirements (base and current)
    """
    # Update repo cache.
    if update_cache:
        debian_update()

    install_command = 'apt-get -y -q install'
    install_packages(install_command,
                     'debian-requirements.txt',
                     package_list=package_list,
                     use_sudo=True)
    uwsgi_install()
    yuglify_install()


@task
def debian_upgrade():
    """
    Upgrade all installed debian/ubuntu packages.
    """
    # Update repo cache
    debian_update()

    # Upgrade packages
    upgrade_ret = sudo('apt-get --force-yes -y upgrade')
    if upgrade_ret.failed:
        raise RuntimeError('Unable to upgrade apt packages.')


@task
def debian_upgrade_reboot():
    """
    debian_upgrade() + reboot for first time/kernel installs.
    """
    debian_upgrade()
    if exists('/var/run/reboot-required', True):
        reboot(REBOOT_TIME)


@task
@log_call
def uwsgi_install(launch_uwsgi=False):
    """
    Installs uwsgi LTS release
    """
    sudo('pip install uwsgi==2.0.14')
    try:
        sudo('rm /usr/bin/uwsgi')
    except:
        pass

    sudo('ln -s /usr/local/bin/uwsgi /usr/bin/uwsgi')
    if launch_uwsgi:
        start_service(env.uwsgi_name)


@task
@log_call
def init_daemon_install():
    """
    Switch from upstart to systemd
    """
    ret = sudo('stat /proc/1/exe')
    if 'upstart' in ret:
        sudo('apt-get -y install systemd-sysv ubuntu-standard')
        sudo('update-initramfs -u')
        reboot(REBOOT_TIME)


@task
@log_call
def locales_install():
    """
    Setup locales (for postgres)
    """
    sudo('locale-gen --purge  en_US en_US.UTF-8')
    sudo('echo -e \'LANG="en_US.UTF-8"\nLANGUAGE="en_US:en"\n\' > /etc/default/locale')
    # run_check('echo export LC_ALL="en_US.UTF-8" >> ~/.bashrc')
    # sudo('locale-gen en_US en_US.UTF-8')
    # sudo('dpkg-reconfigure locales')


@task
@log_call
def redis_install():
    """
    Installs redis
    """
    with cd('/tmp'):
        run('wget http://download.redis.io/releases/redis-stable.tar.gz')
        run('tar xzf redis-stable.tar.gz')
        with cd('redis-stable'):
            run('make')
            sudo('make install')
            with cd('utils'):
                sudo('echo -n | ./install_server.sh')
    start_redis()

@task
@log_call
def rabbitmq_install():
    """
    Installs RabbitMQ
    """
    sudo('/bin/sh -c "wget -qO - https://www.rabbitmq.com/rabbitmq-release-signing-key.asc | apt-key add -"')
    sudo('/bin/sh -c \'echo "deb http://www.rabbitmq.com/debian/ testing main" | tee -a /etc/apt/sources.list.d/rabbitmq.list\'')

    sudo('apt-get update')
    sudo('apt-get --yes --force-yes install rabbitmq-server')

    sudo('rabbitmqctl add_user contrax1 contrax1')
    sudo('rabbitmqctl add_vhost contrax1_vhost')
    sudo('rabbitmqctl set_permissions -p contrax1_vhost contrax1 ".*" ".*" ".*"')


@task
@log_call
def yuglify_install():
    sudo('npm -g install yuglify')
    sudo('ln -s /usr/bin/nodejs /usr/bin/node')


@task
@log_call
def java_install():
    """
    Installs java
    """
    sudo('apt-get install -y python-software-properties debconf-utils software-properties-common')
    sudo('add-apt-repository -y ppa:webupd8team/java')
    sudo('apt-get update')
    sudo('echo "oracle-java8-installer shared/accepted-oracle-license-v1-1 select true" | '
         'debconf-set-selections')
    sudo('apt-get install -y oracle-java8-installer')
    run('java -version')


@task
@log_call
def elasticsearch_install():
    """
    Install and run elasticsearch
    """
    sudo('/bin/sh -c "wget -qO - https://artifacts.elastic.co/GPG-KEY-elasticsearch | apt-key add -"')

    # If everything start crashing: sudo apt remove --purge elasticsearch
    sudo('/bin/sh -c \'echo "deb https://artifacts.elastic.co/packages/6.x/apt stable main" '
         '| tee -a /etc/apt/sources.list.d/elastic-6.x.list\'')
    sudo('apt-get update')
    sudo('apt-get --yes --force-yes install elasticsearch')
    sudo('systemctl daemon-reload')
    sudo('systemctl enable elasticsearch.service')
    restart_service('elasticsearch')


@task
@log_call
def ssl_install():
    """
    Setup SSL certificates
    """
    if env.get('https_redirect'):
        sudo('letsencrypt certonly --email %s'
             ' --text --agree-tos -d %s' % (env.cert_email, env.dns_name))


@task
@log_call
def nltk_download():
    """
    Download nltk data
    """
    with cd(env.project_dir):
        sudo('{} -m nltk.downloader averaged_perceptron_tagger punkt stopwords '
             ' words maxent_ne_chunker wordnet'.format(env.python_bin))


@task
@log_call
def postgres_create():
    """
    Create postgres objects, including owner, databases, and schemas.
    """
    if not pg_user_exists(env.db_user):
        create_pg_user(env.db_user, password=env.db_password)

    if not database_exists(env.db_name):
        create_database(env.db_name, owner=env.db_user)


def clean_base_directory():
    """
    Clean the base directory.
    """
    # TODO: Implement.
    raise NotImplementedError('clean_base_directory() not implemented.')


"""
--------------------------------
Helpers
--------------------------------
"""


@task
def run(command, show=True, *args, **kwargs):
    """
    Runs a shell command on the remote server.
    """
    if show:
        print_command(command)
    with hide("running"):
        return _run(command, *args, **kwargs)


@task
def sudo(command, show=True, *args, **kwargs):
    """
    Runs a command as sudo on the remote server.
    """
    if show:
        print_command(command)
    with hide("running"):
        return _sudo(command, *args, **kwargs)


@task
def print_ssh_key():
    """
    Output id_rsa.pub
    """
    run('cat ~/.ssh/id_rsa.pub')


@task
def print_env(attr_name=None):
    """
    Output env or it's attr.
    """
    print(getattr(env, attr_name) if attr_name else env)


@task
def print_base_dir():
    """
    Output BASE_DIR.
    """
    print(env.base_dir)


@task
def print_config_dir():
    """
    Output CONFIG_DIR.
    """
    print(env.config)


@task
def print_git_creds():
    """
    Output Git credentials.
    """
    print('Git URI: {}'.format(env.git_uri))


@task
def print_git_branch():
    """
    Output Git config
    """
    print('Git branch: {}'.format(env.git_branch))


@task
def print_db_creds():
    """
    Output database credentials
    """
    print('DATABASE_NAME: {}'.format(env.db_name))
    print('POSTGRES_USER: {}'.format(env.db_user))
    print('POSTGRES_PASSWORD: {}'.format(env.db_password))


@task
def get_db_backup():
    """
    Backup db and download the backup archive to local machine.
    """
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d-%H%M")
    file_name = 'db_backup_{}.sql.tgz'.format(timestamp)
    backup_dir = os.path.join(env.base_dir, 'backups')
    mkdir(backup_dir)
    backup_file = os.path.join(backup_dir, file_name)
    backup_cmd = 'PGPASSWORD={db_password} /usr/bin/pg_dump -Ft -v -b -c -O' \
                 ' -h{db_host} -p{db_port} -U{db_user}' \
                 ' -w -f{backup_file} {db_name}'.format(
        db_host=env.db_host,
        db_port=env.db_port,
        db_user=env.db_user,
        db_name=env.db_name,
        db_password=env.db_password,
        backup_file=backup_file)
    run(backup_cmd)
    get(backup_file, env.config_dir)
    run('rm %s' % backup_file)


@task
def kill(process_name):
    """
    Kill process by name
    """
    run('pkill -f %s' % process_name)


@task
def kill_tika():
    """
    Kill Tika process
    """
    kill('TikaServer')


@task
def ssh_agent_remove_key():
    run_check('ssh-add -d ~/.ssh/id_rsa')


@task
def local():
    env.run = _local
    env.hosts = ['localhost']


@task
def create_superuser():
    """
    Create superuser
    :return:
    """
    if env.get('superuser_username'):
        manage('create_superuser --username {} --password {} --email {}'.format(
            env.superuser_username,
            env.superuser_password,
            env.superuser_email
        ))


@contextmanager
def virtualenv():
    """
    Runs commands within the project's virtualenv.
    """
    with cd(env.base_dir):
        with prefix("source %s/activate" % env.ve_bin):
            yield


@task
@log_call
def jqwidgets_install():
    if env.get('jqwidgets_zip_archive_path'):
        # if not localhost copy archive to a remote /tmp
        if env.host != 'localhost':
            put(env.jqwidgets_zip_archive_path, '/tmp')
            env.jqwidgets_zip_archive_path = os.path.join(
                '/tmp', os.path.basename(env.jqwidgets_zip_archive_path))
        # unzip
        run('unzip {zip_file_path} "jqwidgets/*" -d {dest_dir}'.format(
            zip_file_path=env.jqwidgets_zip_archive_path,
            dest_dir=os.path.join(STATICFILES_DIR, 'vendor')))
    else:
        print(red('No "jqwidgets_zip_archive_path" fabricrc setting specified, skip.'))
        print(yellow('WARNING: install that dependence separately. See project documentation.'))


@task
@log_call
def theme_install():
    if env.get('theme_zip_archive_path'):
        # if not localhost copy archive to a remote /tmp
        if env.host != 'localhost':
            put(env.theme_zip_archive_path, '/tmp')
            env.theme_zip_archive_path = os.path.join(
                '/tmp', os.path.basename(env.theme_zip_archive_path))
        # create destination directory
        tmp_dir = '/tmp/theme'
        dest_dir = os.path.join(STATICFILES_DIR, 'theme')
        run('mkdir -p {}'.format(dest_dir))
        sources_path = 'Package-HTML/HTML'
        container = os.path.join(tmp_dir, sources_path)
        # unpack and place in static dir these folders:
        required_sources = ['js', 'css', 'images']
        for source in required_sources:
            run('unzip {zip_file_path} "{source}" -d {tmp_dir}'.format(
                zip_file_path=env.theme_zip_archive_path,
                source=os.path.join(sources_path, source, '*'), # e.g. Package-HTML/HTML/js/*
                tmp_dir=tmp_dir))                               # e.g. /tmp/theme
            run('cp -r {_from} {_to}'.format(
                _from=os.path.join(container, source),          # e.g. /tmp/theme/Package-HTML/HTML/js
                _to=dest_dir                                    # e.g. staticfiles/theme
            ))
        # unpack style.css and place it in theme/css static folder
        run('unzip -j {zip_file_path} "{source}" -d {dest_dir}'.format(
           zip_file_path=env.theme_zip_archive_path,
           source=os.path.join(sources_path, 'style.css'),
           dest_dir=os.path.join(dest_dir, 'css')))
        sudo('rm -r {}'.format(tmp_dir))
    else:
        print(red('No "theme_zip_archive_path" fabricrc setting specified, skip.'))
        print(yellow('WARNING: install that dependence separately. See project documentation.'))


@task
def stanford_install():
    with virtualenv():
        result = run('pip show lexnlp', warn_only=True)
        if result == '':
            print(yellow('WARNING!!! Lexnlp package is not installed.'))
            # return
        # lexnlp_location = re.findall(r'Location:\s(.+?)$', result, re.M)[0].strip()

    lexnlp_location = '/usr'
    libs_path = os.path.join(lexnlp_location, 'lexnlp', 'libs')
    mkdir(libs_path, use_sudo=True)

    stanford_version = "2017-06-09"
    stanford_path = os.path.join(libs_path, "stanford_nlp")
    stanford_urls = [
        'https://nlp.stanford.edu/software/stanford-corenlp-full-{}.zip',
        'https://nlp.stanford.edu/software/stanford-parser-full-{}.zip',
        'https://nlp.stanford.edu/software/stanford-english-corenlp-{}-models.jar',
        'https://nlp.stanford.edu/software/stanford-postagger-full-{}.zip',
        'https://nlp.stanford.edu/software/stanford-ner-{}.zip',
    ]
    for url in stanford_urls:
        url = url.format(stanford_version)
        run('wget --continue -O tmp.zip "{}"'.format(url))
        run('unzip tmp.zip -d {}'.format(stanford_path))
        run('rm -f tmp.zip')

# coding=utf-8
from subprocess import check_output
import sys
import os
import logging
import site

from setuptools import find_packages
from setuptools import setup

assert sys.version_info[0] == 3 and sys.version_info[1] >= 6, "hive requires Python 3.6 or newer"

# PEP517 workaround
# see https://github.com/pypa/pip/issues/7953#issuecomment-645133255
site.ENABLE_USER_SITE = "--user" in sys.argv[1:]

VERSION = 'notag'
GIT_REVISION = 'nogitrev'
GIT_DATE = 'nogitdate'
class GitRevisionProvider(object):
    """ Static class to provide version and git revision information"""
    logger = logging.getLogger('GitRevisionProvider')

    @classmethod
    def is_git_sha(cls, s):
        from re import fullmatch
        return fullmatch('^g[0-9a-f]{8}$', s) is not None

    @classmethod
    def get_git_revision(cls, s):
        git_revision = str(GIT_REVISION)
        if cls.is_git_sha(s):
            git_revision = s.lstrip('g')
        return git_revision

    @classmethod
    def get_commits_count(cls, s):
        commits = None
        try:
            commits = int(s)
        except:
            pass
        return commits

    @classmethod
    def get_git_date(cls, commit):
        if commit == GIT_REVISION:
            return GIT_DATE
        command = "git show -s --format=%ci {}".format(commit)
        hivemind_git_date_string = check_output(command.split()).decode('utf-8').strip()
        return hivemind_git_date_string

    @classmethod
    def provide_git_revision(cls):
        """ Evaluate version and git revision and save it to a version file
            Evaluation is based on VERSION variable and git describe if
            .git directory is present in tree.
            In case when .git is not available version and git_revision is taken
            from get_distribution call

        """
        version = str(VERSION)
        git_revision = str(GIT_REVISION)
        git_date = str(GIT_DATE)
        if os.path.exists(".git"):
            from subprocess import check_output
            command = 'git describe --tags --long --dirty'
            version_string = check_output(command.split()).decode('utf-8').strip()
            if version_string != 'fatal: No names found, cannot describe anything.':
                # git describe -> tag-commits-sha-dirty
                version_string = version_string.replace('-dirty', '')
                version_string = version_string.lstrip('v')
                parts = version_string.split('-')
                parts_len = len(parts)
                # only tag or git sha
                if parts_len == 1:
                    if cls.is_git_sha(parts[0]):
                        git_revision = parts[0]
                        git_revision = git_revision.lstrip('g')
                    else:
                        version = parts[0]
                if parts_len == 2:
                    version = parts[0]
                    git_revision = cls.get_git_revision(parts[1])
                if parts_len > 2:
                    # git sha
                    git_revision = cls.get_git_revision(parts[-1])
                    # commits after given tag
                    commits = cls.get_commits_count(parts[-2])
                    # version based on tag
                    version = ''.join(parts[:-1])
                    if commits is not None:
                        version = ''.join(parts[:-2])
                    # normalize rc to rcN for PEP 440 compatibility
                    version = version.lower()
                    if version.endswith('rc'):
                        version += '0'
            else:
                cls.logger.warning("Git describe command failed for current git repository")
            git_date = cls.get_git_date(git_revision)
        else:
            from pkg_resources import get_distribution
            try:
                version, git_revision = get_distribution("hivemind").version.split("+")
            except:
                cls.logger.warning("Unable to get version and git revision from package data")
        cls._save_version_file(version, git_revision, git_date)
        return version, git_revision

    @classmethod
    def _save_version_file(cls, hivemind_version, git_revision, git_date):
        """ Helper method to save version.py with current version and git_revision """
        with open("hive/version.py", 'w') as version_file:
            version_file.write("# generated by setup.py\n")
            version_file.write("# contents will be overwritten\n")
            version_file.write("VERSION = '{}'\n".format(hivemind_version))
            version_file.write("GIT_REVISION = '{}'\n".format(git_revision))
            version_file.write("GIT_DATE = '{}'\n".format(git_date))

VERSION, GIT_REVISION = GitRevisionProvider.provide_git_revision()
SQL_SCRIPTS_PATH = 'hive/db/sql_scripts/'
SQL_UPGRADE_PATH = 'hive/db/sql_scripts/upgrade/'

def get_sql_scripts(dir, base_dir):
    from os import listdir
    from os.path import isfile, join, relpath
    if base_dir is None:
        return [join(dir, f) for f in listdir(dir) if isfile(join(dir, f))]
    else:
        return [relpath(join(dir, f), base_dir) for f in listdir(dir) if isfile(join(dir, f))]

if __name__ == "__main__":

    sql_scripts = get_sql_scripts(SQL_SCRIPTS_PATH, "hive/db/")
    sql_upgrade_scripts = get_sql_scripts(SQL_UPGRADE_PATH, "hive/db/")
    
    print('Found {} SQL scripts to be installed.'.format(len(sql_scripts)))
    print('Found {} upgrade SQL scripts to be installed.'.format(len(sql_upgrade_scripts)))

    for s in sql_scripts:
        print("Found SQL script: {}".format(s))

    for s in sql_upgrade_scripts:
        print("Found SQL upgrade script: {}".format(s))

    package_resources = {"hive.db": sql_scripts + sql_upgrade_scripts}
    found_packages = find_packages(exclude=['scripts'])

    for p in found_packages:
        print("Found Python package: {}".format(p))


    setup(
        name='hivemind',
        version=VERSION + "+" + GIT_REVISION,
        description='Developer-friendly microservice powering social networks on the Hive blockchain.',
        long_description=open('README.md').read(),
        packages=found_packages,
        package_data=package_resources,
        setup_requires=[
            'pytest-runner'
        ],
        install_requires=[
            'aiopg==1.2.1',
            'jsonrpcserver==4.2.0',
            'simplejson==3.17.2',
            'aiohttp==3.7.4',
            'certifi==2020.12.5',
            'sqlalchemy==1.4.15',
            'funcy==1.16',
            'toolz==0.11.1',
            'maya==0.6.1',
            'ujson==4.0.2',
            'urllib3==1.26.5',
            'psycopg2-binary==2.8.6',
            'aiocache==0.11.1',
            'configargparse==1.4.1',
            'pdoc==0.3.2',
            'diff-match-patch==20200713',
            'prometheus-client==0.10.1',
            'psutil==5.8.0',
            'atomic==0.7.3',
            'python-dateutil==2.8.1',
            'regex==2021.4.4'
        ],
        extras_require={
            'dev': [
                'pyYAML',
                'prettytable'
            ]
        },
        entry_points={
            'console_scripts': [
                'hive=hive.cli:run',
            ]
        }
    )

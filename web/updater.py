# Copyright (C) 2013  Stefano Zacchiroli <zack@upsilon.cc>
#
# This file is part of Debsources.
#
# Debsources is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option) any
# later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU Affero General Public License for more
# details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import logging
import os
import subprocess

from datetime import datetime
from email.utils import formatdate
from sqlalchemy import func as sql_func

import dbutils
import fs_storage

from debmirror import SourceMirror, SourcePackage
from models import Metric, Version

KNOWN_EVENTS = [ 'add-package', 'rm-package' ]
NO_OBSERVERS = dict( [ (e, []) for e in KNOWN_EVENTS ] )


# TODO fill tables: BinaryPackage, BinaryVersion, SuitesMapping
# TODO get rid of shell hooks; they shall die a horrible death

def notify(observers, conf, event, session, pkg, pkgdir):
    """notify (Python and shell) hooks of occurred events

    Currently supported events:

    * add-package: a package is being added to Debsources; its source files
      have already been unpacked to the file storage and its metadata have
      already been added to the database

    * rm-package: a package is being removed from Debsources; its source files
      are still part of the file storage and its metadata are still part of the
      database

    Python hooks are passed the following arguments, in this order:

    * session: ongoing database session; failures in hook execution will cause
      the session to be rolled back, udoing pending database modifications
      (e.g. the addition/removal of package metadata)

    * pkg: a debmirror.SourcePackage representation of the package being acted
      upon

    * pkgdir: path pointing to the package location in the file storage

    Shell hoks re invoked with the following arguments: pkgdir, package name,
    package version

    """
    logging.debug('notify %s for %s' % (event, pkg))
    package, version = pkg['package'], pkg['version']
    cmd = ['run-parts', '--exit-on-error',
           '--arg', pkgdir,
           '--arg', package,
           '--arg', version,
           os.path.join(conf['bin_dir'], event + '.d')
       ]

    # fire shell hooks
    try:
        subprocess.check_output(cmd, stderr=subprocess.STDOUT)
    except subprocess.CalledProcessError, e:
        logging.error('shell hooks for %s on %s returned exit code %d. Output: %s'
                      % (event, pkg, e.returncode, e.output))
        raise e

    notify_plugins(observers, event, session, pkg, pkgdir)


def notify_plugins(observers, event, session, pkg, pkgdir,
                   triggers=None, dry=False):
    """notify Python hooks of occurred events

    If triggers is not None, only Python hooks whose names are listed in them
    will be triggered. Note: shell hooks will not be triggered in that case.
    """
    for (title, action) in observers[event]:
        try:
            if triggers is None:
                action(session, pkg, pkgdir)
            elif (event, title) in triggers:
                logging.info('notify (forced) %s/%s for %s' % (event, title, pkg))
                if not dry:
                    action(session, pkg, pkgdir)
        except:
            logging.error('plugin hooks for %s on %s failed' % (event, pkg))
            raise


def extract_new(conf, session, mirror, observers=NO_OBSERVERS, dry=False):
    """update phase 1: list mirror and extract new packages

    """
    logging.info('add new packages...')
    src_list_path = os.path.join(conf['cache_dir'], 'sources.txt')
    src_list = open(src_list_path + '.new', 'w')
    for pkg in mirror.ls():
        pkgdir = pkg.extraction_dir(conf['sources_dir'])
        if not dbutils.lookup_version(session, pkg['package'], pkg['version']):
            try:
                logging.info('add %s...' % pkg)
                if not dry and 'fs' in conf['passes']:
                    fs_storage.extract_package(pkg, pkgdir)
                with session.begin_nested():
                    # single db session for package addition and hook
                    # execution: if the hooks fail, the package won't be
                    # added to the db (it will be tried again at next run)
                    if not dry and 'db' in conf['passes']:
                        dbutils.add_package(session, pkg)
                    if not dry and 'hooks' in conf['passes']:
                        notify(observers, conf,
                               'add-package', session, pkg, pkgdir)
            except:
                logging.exception('failed to extract %s' % pkg)
        if conf['force_triggers']:
            try:
                notify_plugins(observers, 'add-package', session, pkg, pkgdir,
                               triggers=conf['force_triggers'], dry=dry)
            except:
                logging.exception('trigger failure on %s' % pkg)
        src_list.write('%s\t%s\t%s\t%s\t%s\n' %
                       (pkg['package'], pkg['version'], pkg.archive_area(),
                        pkg.dsc_path(), pkgdir))
    src_list.close()
    os.rename(src_list_path + '.new', src_list_path)


def garbage_collect(conf, session, mirror, observers=NO_OBSERVERS, dry=False):
    """update phase 2: list db and remove disappeared and expired packages

    """
    logging.info('garbage collection...')
    for version in session.query(Version).all():
        pkg = SourcePackage.from_db_model(version)
        pkg_id = (pkg['package'], pkg['version'])
        pkgdir = pkg.extraction_dir(conf['sources_dir'])
        if not pkg_id in mirror.packages:
            # package is in in Debsources db, but gone from mirror: we
            # might have to garbage collect it (depending on expiry)
            try:
                expire_days = conf['expire_days']
                age = None
                if os.path.exists(pkgdir):
                    age = datetime.now() \
                          - datetime.fromtimestamp(os.path.getmtime(pkgdir))
                if not age or age.days >= expire_days:
                    logging.info("gc %s..." % pkg)
                    if not dry and 'hooks' in conf['passes']:
                        notify(conf, 'rm-package', session, pkg, pkgdir)
                    if not dry and 'fs' in conf['passes']:
                        fs_storage.remove_package(pkg, pkgdir)
                    if not dry and 'db' in conf['passes']:
                        with session.begin_nested():
                            dbutils.rm_package(session, pkg, version)
                else:
                    logging.debug('not removing %s as it is too young' % pkg)
            except:
                logging.exception('failed to remove %s' % pkg)
        if conf['force_triggers']:
            try:
                notify_plugins(observers, 'rm-package', session, pkg, pkgdir,
                               triggers=conf['force_triggers'], dry=dry)
            except:
                logging.exception('trigger failure on %s' % pkg)


def update_metadata(conf, session, dry=False):
    """update phase 3: update metadata and cached values

    """
    # TODO 'dry' argument is currently unused in this function

    # update global stats file (most notably: size info in it)
    stats_file = os.path.join(conf['cache_dir'], 'stats.data')
    total_size = session.query(sql_func.sum(Metric.value)) \
                        .filter_by(metric='size').first()[0]
    if not total_size:
        total_size = 0
    with open(stats_file, 'w') as out:
        out.write('%s\t%d\n' % ('size', total_size))

    # update package prefixes list
    with open(os.path.join(conf['cache_dir'], 'pkg-prefixes'), 'w') as out:
        for prefix in SourceMirror(conf['mirror_dir']).pkg_prefixes():
            out.write('%s\n' % prefix)

    # update timestamp
    timestamp_file = os.path.join(conf['cache_dir'], 'last-update')
    with open(timestamp_file, 'w') as out:
        out.write('%s\n' % formatdate())


def update(conf, session, observers=NO_OBSERVERS):
    """do a full update run
    """
    dry = conf['dry_run']

    logging.info('start')
    logging.info('list mirror packages...')
    mirror = SourceMirror(conf['mirror_dir'])

    extract_new(conf, session, mirror, observers, dry)		# phase 1
    garbage_collect(conf, session, mirror, observers, dry)	# phase 2
    update_metadata(conf, session, dry)				# phase 3

    logging.info('finish')

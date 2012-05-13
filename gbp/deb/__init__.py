# vim: set fileencoding=utf-8 :
#
# (C) 2006,2007,2011 Guido Günther <agx@sigxcpu.org>
#    This program is free software; you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation; either version 2 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with this program; if not, write to the Free Software
#    Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA
"""provides some debian source package related helpers"""

import commands
import email
import os
import re
import subprocess
import sys
import glob

import gbp.command_wrappers as gbpc
from gbp.errors import GbpError
from gbp.git import GitRepositoryError
from gbp.deb.changelog import ChangeLog, NoChangeLogError
from gbp.pkg import (PkgPolicy, UpstreamSource, compressor_opts)

# When trying to parse a version-number from a dsc or changes file, these are
# the valid characters.
debian_version_chars = 'a-zA-Z\d.~+-'


class DebianPkgPolicy(PkgPolicy):
    """Packaging policy for Debian"""

    # Valid package names according to Debian Policy Manual 5.6.1:
    # "Package names (both source and binary, see Package, Section 5.6.7)
    # must consist only of lower case letters (a-z), digits (0-9), plus (+)
    # and minus (-) signs, and periods (.). They must be at least two
    # characters long and must start with an alphanumeric character."
    packagename_re = re.compile("^[a-zA-Z0-9][a-zA-Z0-9\.\+\-~]+$")
    packagename_msg = """Package names must be at least two characters long, start with an
    alphanumeric and can only containg letters (a-z,A-Z), digits
    (0-9), plus signs (+), minus signs (-), periods (.) and hyphens (~)"""

    # Valid upstream versions according to Debian Policy Manual 5.6.12:
    # "The upstream_version may contain only alphanumerics[32] and the
    # characters . + - : ~ (full stop, plus, hyphen, colon, tilde) and
    # should start with a digit. If there is no debian_revision then hyphens
    # are not allowed; if there is no epoch then colons are not allowed."
    # Since we don't know about any epochs and debian revisions yet, the
    # last two conditions are not checked.
    upstreamversion_re = re.compile("^[0-9][a-z0-9\.\+\-\:\~]*$")
    upstreamversion_msg = """Upstream version numbers must start with a digit and can only containg lower case
    letters (a-z), digits (0-9), full stops (.), plus signs (+), minus signs
    (-), colons (:) and tildes (~)"""

    @staticmethod
    def build_tarball_name(name, version, compression, dir=None):
        """
        Given a source package's I{name}, I{version} and I{compression}
        return the name of the corresponding upstream tarball.

        >>> DebianPkgPolicy.build_tarball_name('foo', '1.0', 'bzip2')
        'foo_1.0.orig.tar.bz2'
        >>> DebianPkgPolicy.build_tarball_name('bar', '0.0~git1234', 'xz')
        'bar_0.0~git1234.orig.tar.xz'

        @param name: the source package's name
        @type name: C{str}
        @param version: the upstream version
        @type version: C{str}
        @param compression: the desired compression
        @type compression: C{str}
        @param dir: a directory to prepend
        @type dir: C{str}
        @return: the tarballs name corresponding to the input parameters
        @rtype: C{str}
        """
        ext = compressor_opts[compression][1]
        tarball = "%s_%s.orig.tar.%s" % (name, version, ext)
        if dir:
            tarball = os.path.join(dir, tarball)
        return tarball


class DpkgCompareVersions(gbpc.Command):
    cmd='/usr/bin/dpkg'

    def __init__(self):
        if not os.access(self.cmd, os.X_OK):
            raise GbpError, "%s not found - cannot use compare versions" % self.cmd
        gbpc.Command.__init__(self, self.cmd, ['--compare-versions'])

    def __call__(self, version1, version2):
        self.run_error = "Couldn't compare %s with %s" % (version1, version2)
        res = gbpc.Command.call(self, [ version1, 'lt', version2 ])
        if res not in [ 0, 1 ]:
            raise gbpc.CommandExecFailed, "%s: bad return code %d" % (self.run_error, res)
        if res == 0:
            return -1
        elif res == 1:
            res = gbpc.Command.call(self, [ version1, 'gt', version2 ])
            if res not in [ 0, 1 ]:
                raise gbpc.CommandExecFailed, "%s: bad return code %d" % (self.run_error, res)
            if res == 0:
                return 1
        return 0


class DscFile(object):
    """Keeps all needed data read from a dscfile"""
    compressions = r"(%s)" % '|'.join(UpstreamSource.known_compressions())
    pkg_re = re.compile(r'Source:\s+(?P<pkg>.+)\s*')
    version_re = re.compile(r'Version:\s((?P<epoch>\d+)\:)?(?P<version>[%s]+)\s*$' % debian_version_chars)
    tar_re = re.compile(r'^\s\w+\s\d+\s+(?P<tar>[^_]+_[^_]+(\.orig)?\.tar\.%s)' % compressions)
    diff_re = re.compile(r'^\s\w+\s\d+\s+(?P<diff>[^_]+_[^_]+\.diff.(gz|bz2))')
    deb_tgz_re = re.compile(r'^\s\w+\s\d+\s+(?P<deb_tgz>[^_]+_[^_]+\.debian.tar.%s)' % compressions)
    format_re = re.compile(r'Format:\s+(?P<format>[0-9.]+)\s*')

    def __init__(self, dscfile):
        self.pkg = ""
        self.tgz = ""
        self.diff = ""
        self.deb_tgz = ""
        self.pkgformat = "1.0"
        self.debian_version = ""
        self.upstream_version = ""
        self.native = False
        self.dscfile = os.path.abspath(dscfile)

        f = file(self.dscfile)
        fromdir = os.path.dirname(os.path.abspath(dscfile))
        for line in f:
            m = self.version_re.match(line)
            if m and not self.upstream_version:
                if '-' in m.group('version'):
                    self.debian_version = m.group('version').split("-")[-1]
                    self.upstream_version = "-".join(m.group('version').split("-")[0:-1])
                    self.native = False
                else:
                    self.native = True # Debian native package
                    self.upstream_version = m.group('version')
                if m.group('epoch'):
                    self.epoch = m.group('epoch')
                else:
                    self.epoch = ""
                continue
            m = self.pkg_re.match(line)
            if m:
                self.pkg = m.group('pkg')
                continue
            m = self.deb_tgz_re.match(line)
            if m:
                self.deb_tgz = os.path.join(fromdir, m.group('deb_tgz'))
                continue
            m = self.tar_re.match(line)
            if m:
                self.tgz = os.path.join(fromdir, m.group('tar'))
                continue
            m = self.diff_re.match(line)
            if m:
                self.diff = os.path.join(fromdir, m.group('diff'))
                continue
            m = self.format_re.match(line)
            if m:
                self.pkgformat = m.group('format')
                continue
        f.close()

        if not self.pkg:
            raise GbpError, "Cannot parse package name from '%s'" % self.dscfile
        elif not self.tgz:
            raise GbpError, "Cannot parse archive name from '%s'" % self.dscfile
        if not self.upstream_version:
            raise GbpError, "Cannot parse version number from '%s'" % self.dscfile
        if not self.native and not self.debian_version:
            raise GbpError, "Cannot parse Debian version number from '%s'" % self.dscfile

    def _get_version(self):
        version = [ "", self.epoch + ":" ][len(self.epoch) > 0]
        if self.native:
            version += self.upstream_version
        else:
            version += "%s-%s" % (self.upstream_version, self.debian_version)
        return version

    version = property(_get_version)

    def __str__(self):
        return "<%s object %s>" % (self.__class__.__name__, self.dscfile)


def parse_dsc(dscfile):
    """parse dsc by creating a DscFile object"""
    try:
        dsc = DscFile(dscfile)
    except IOError, err:
        raise GbpError, "Error reading dsc file: %s" % err

    return dsc

def parse_changelog_repo(repo, branch, filename):
    """
    Parse the changelog file from given branch in the git
    repository.
    """
    try:
        # Note that we could just pass in the branch:filename notation
        # to show as well, but we want to check if the branch / filename
        # exists first, so we can give a separate error from other
        # repository errors.
        sha = repo.rev_parse("%s:%s" % (branch, filename))
    except GitRepositoryError:
        raise NoChangeLogError, "Changelog %s not found in branch %s" % (filename, branch)

    lines = repo.show(sha)
    return ChangeLog('\n'.join(lines))


def orig_file(cp, compression):
    """
    The name of the orig file belonging to changelog cp

    >>> orig_file({'Source': 'foo', 'Upstream-Version': '1.0'}, "bzip2")
    'foo_1.0.orig.tar.bz2'
    >>> orig_file({'Source': 'bar', 'Upstream-Version': '0.0~git1234'}, "xz")
    'bar_0.0~git1234.orig.tar.xz'
    """
    return DebianPkgPolicy.build_tarball_name(cp['Source'],
                                              cp['Upstream-Version'],
                                              compression)


def parse_uscan(out):
    """
    Parse the uscan output return (True, tarball) if a new version was
    downloaded and could be located. If the tarball can't be located it returns
    (True, None). Returns (False, None) if the current version is up to date.

    >>> parse_uscan("<status>up to date</status>")
    (False, None)
    >>> parse_uscan("<target>virt-viewer_0.4.0.orig.tar.gz</target>")
    (True, '../virt-viewer_0.4.0.orig.tar.gz')

    @param out: uscan output
    @type out: string
    @return: status and tarball name
    @rtype: tuple
    """
    source = None
    if "<status>up to date</status>" in out:
        return (False, None)
    else:
        # Check if uscan downloaded something
        for row in out.split("\n"):
            # uscan >= 2.10.70 has a target element:
            m = re.match(r"<target>(.*)</target>", row)
            if m:
                source = '../%s' % m.group(1)
                break
            elif row.startswith('<messages>'):
                m = re.match(r".*symlinked ([^\s]+) to it", row)
                if m:
                    source = "../%s" % m.group(1)
                    break
                m = re.match(r"Successfully downloaded updated package ([^<]+)", row)
                if m:
                    source = "../%s" % m.group(1)
                    break
        # try to determine the already downloaded sources name
        else:
            d = {}
            for row in out.split("\n"):
                for n in ('package', 'upstream-version', 'upstream-url'):
                    m = re.match("<%s>(.*)</%s>" % (n,n), row)
                    if m:
                        d[n] = m.group(1)
            d["ext"] = os.path.splitext(d['upstream-url'])[1]
            # We want the name of the orig tarball if possible
            source = "../%(package)s_%(upstream-version)s.orig.tar%(ext)s" % d
            if not os.path.exists(source):
                # Fall back to the sources name otherwise
                source = "../%s" % d['upstream-url'].rsplit('/',1)[1]
                print source
                if not os.path.exists(source):
                    source = None
        return (True, source)


def do_uscan():
    """invoke uscan to fetch a new upstream version"""
    p = subprocess.Popen(['uscan', '--symlink', '--destdir=..', '--dehs'], stdout=subprocess.PIPE)
    out = p.communicate()[0]
    return parse_uscan(out)


def get_arch():
    pipe = subprocess.Popen(["dpkg", "--print-architecture"], shell=False, stdout=subprocess.PIPE)
    arch = pipe.stdout.readline().strip()
    return arch


def compare_versions(version1, version2):
    """compares to Debian versionnumbers suitable for sort()"""
    return DpkgCompareVersions()(version1, version2)


# vim:et:ts=4:sw=4:et:sts=4:ai:set list listchars=tab\:»·,trail\:·:
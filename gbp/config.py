# vim: set fileencoding=utf-8 :
#
# (C) 2006,2007,2010-2012,2015 Guido Guenther <agx@sigxcpu.org>
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
#    along with this program; if not, please see
#    <http://www.gnu.org/licenses/>
"""handles command line and config file option parsing for the gbp commands"""

from optparse import OptionParser, OptionGroup, Option, OptionValueError
from six.moves import configparser
from copy import copy
import errno
import os.path
import sys
import tempfile


try:
    from gbp.version import gbp_version
except ImportError:
    gbp_version = "[Unknown version]"
import gbp.tristate
import gbp.log
from gbp.git import GitRepositoryError, GitRepository

no_upstream_branch_msg = """
Repository does not have branch '%s' for upstream sources. If there is none see
file:///usr/share/doc/git-buildpackage/manual-html/gbp.import.html#GBP.IMPORT.CONVERT
on howto create it or check the --create-missing-branches option. Otherwise,
use --upstream-branch to specify it.
"""

def expand_path(option, opt, value):
    value = os.path.expandvars(value)
    return os.path.expanduser(value)

def check_tristate(option, opt, value):
    try:
        val = gbp.tristate.Tristate(value)
    except TypeError:
        raise OptionValueError(
            "option %s: invalid value: %r" % (opt, value))
    else:
        return val


def optparse_split_cb(option, opt_str, value, parser):
    """Split option string into a list"""
    setattr(parser.values, option.dest, value.split(','))


def safe_option(f):
    def _decorator(self, *args, **kwargs):
        obj = self
        option_name = kwargs.get('option_name')
        if not option_name and len(args):
            option_name = args[0]

        # We're decorating GbpOption not GbpOptionParser
        if not hasattr(obj, 'valid_options'):
            if not hasattr(obj, 'parser'):
                raise ValueError("Can only decorete GbpOptionParser and GbpOptionGroup not %s" % obj)
            else:
                obj = obj.parser

        if option_name and not option_name.startswith('no-'):
            obj.valid_options.append(option_name)
        return f(self, *args, **kwargs)
    return _decorator


class GbpOption(Option):
    TYPES = Option.TYPES + ('path', 'tristate')
    TYPE_CHECKER = copy(Option.TYPE_CHECKER)
    TYPE_CHECKER['path'] = expand_path
    TYPE_CHECKER['tristate'] = check_tristate

class GbpOptionParser(OptionParser):
    """
    Handles commandline options and parsing of config files
    @ivar command: the gbp command we store the options for
    @type command: string
    @ivar prefix: prefix to prepend to all commandline options
    @type prefix: string
    @ivar config: current configuration parameters
    @type config: dict
    @cvar defaults: defaults value of an option if not in the config file or
    given on the command line
    @type defaults: dict
    @cvar help: help messages
    @type help: dict
    @cvar def_config_files: config files we parse
    @type def_config_files: dict (type, path)
    """
    defaults = { 'debian-branch'   : 'master',
                 'packaging-branch' : 'master',
                 'upstream-branch' : 'upstream',
                 'upstream-tree'   : 'TAG',
                 'pristine-tar'    : 'False',
                 'pristine-tar-commit': 'False',
                 'filter-pristine-tar' : 'False',
                 'sign-tags'       : 'False',
                 'force-create'    : 'False',
                 'no-create-orig'  : 'False',
                 'cleaner'         : '/bin/true',
                 'keyid'           : '',
                 'posttag'         : '',
                 'postbuild'       : '',
                 'prebuild'        : '',
                 'postexport'      : '',
                 'postimport'      : '',
                 'build'           : 'True',
                 'hooks'           : 'True',
                 'debian-tag'      : 'debian/%(version)s',
                 'debian-tag-msg'  : '%(pkg)s Debian release %(version)s',
                 'upstream-tag'    : 'upstream/%(version)s',
                 'import-msg'      : 'Imported Upstream version %(version)s',
                 'commit-msg'      : 'Update changelog for %(version)s release',
                 'filter'          : [],
                 'snapshot-number' : 'snapshot + 1',
                 'git-log'         : '--no-merges',
                 'export'          : 'HEAD',
                 'export-dir'      : '',
                 'overlay'         : 'False',
                 'tarball-dir'     : '',
                 'ignore-new'      : 'False',
                 'ignore-untracked': 'False',
                 'ignore-branch'   : 'False',
                 'meta'            : 'True',
                 'meta-closes'     : 'Closes|LP',
                 'meta-closes-bugnum'  : r'(?:bug|issue)?\#?\s?\d+',
                 'full'            : 'False',
                 'id-length'       : '0',
                 'git-author'      : 'False',
                 'ignore-regex'    : '',
                 'compression'     : 'auto',
                 'compression-level': '9',
                 'remote-url-pattern' : 'ssh://git.debian.org/git/collab-maint/%(pkg)s.git',
                 'multimaint'      : 'True',
                 'multimaint-merge': 'False',
                 'pbuilder'        : 'False',
                 'qemubuilder'     : 'False',
                 'dist'            : 'sid',
                 'arch'            : '',
                 'interactive'     : 'True',
                 'color'           : 'auto',
                 'color-scheme'   : '',
                 'customizations'  : '',
                 'spawn-editor'    : 'release',
                 'patch-numbers'   : 'True',
                 'patch-num-format': '%04d-',
                 'renumber'        : 'False',
                 'notify'          : 'auto',
                 'merge'           : 'True',
                 'merge-mode'      : 'merge',
                 'track'           : 'True',
                 'author-is-committer': 'False',
                 'author-date-is-committer-date': 'False',
                 'create-missing-branches': 'False',
                 'submodules'      : 'False',
                 'time-machine'    : 1,
                 'pbuilder-autoconf' : 'True',
                 'pbuilder-options': '',
                 'template-dir': '',
                 'remote-config': '',
                 'allow-unauthenticated': 'False',
                 'symlink-orig': 'True',
                 'purge': 'True',
                 'drop': 'False',
                 'commit': 'False',
                 'upstream-vcs-tag': '',
                 'tmp-dir': '/var/tmp/gbp/',
             }
    help = {
             'debian-branch':
                  ("Branch the Debian package is being developed on, "
                   "default is '%(debian-branch)s'"),
             'packaging-branch':
                  ("Branch the packaging is being maintained on, "
                   "rpm counterpart of the 'debian-branch' option, "
                   "default is '%(packaging-branch)s'"),
             'upstream-branch':
                  "Upstream branch, default is '%(upstream-branch)s'",
             'upstream-tree':
                  ("Where to generate the upstream tarball from "
                   "(tag or branch), default is '%(upstream-tree)s'"),
             'debian-tag':
                  ("Format string for debian tags, "
                   "default is '%(debian-tag)s'"),
             'debian-tag-msg':
                  ("Format string for signed debian-tag messages, "
                   "default is '%(debian-tag-msg)s'"),
             'upstream-tag':
                  ("Format string for upstream tags, "
                   "default is '%(upstream-tag)s'"),
             'sign-tags':
                  "Whether to sign tags, default is '%(sign-tags)s'",
             'keyid':
                  "GPG keyid to sign tags with, default is '%(keyid)s'",
             'import-msg':
                  ("Format string for commit message used to commit "
                   "the upstream tarball, default is '%(import-msg)s'"),
             'commit-msg':
                  ("Format string for commit messag used to commit, "
                   "the changelog, default is '%(commit-msg)s'"),
             'pristine-tar':
                  ("Use pristine-tar to create orig tarball, "
                   "default is '%(pristine-tar)s'"),
             'pristine-tar-commit':
                  ("When generating a tarball commit it to the pristine-tar branch '%(pristine-tar-commit)s' "
                   "default is '%(pristine-tar-commit)s'"),
             'filter-pristine-tar':
                  "Filter pristine-tar when filter option is used, default is '%(filter-pristine-tar)s'",
             'filter':
                  "Files to filter out during import (can be given multiple times), default is %(filter)s",
             'git-author':
                  "Use name and email from git-config for changelog trailer, default is '%(git-author)s'",
             'full':
                  "Include the full commit message instead of only the first line, default is '%(full)s'",
             'meta':
                  "Parse meta tags in commit messages, default is '%(meta)s'",
             'meta-closes':
                  "Meta tags for the bts close commands, default is '%(meta-closes)s'",
             'meta-closes-bugnum':
                  "Meta bug number format, default is '%(meta-closes-bugnum)s'",
             'ignore-new':
                  "Build with uncommited changes in the source tree, default is '%(ignore-new)s'",
             'ignore-untracked':
                  "Build with untracked files in the source tree, default is "
                  "'%(ignore-untracked)s'",
             'ignore-branch':
                  ("Build although debian-branch != current branch, "
                   "default is '%(ignore-branch)s'"),
             'overlay':
                  ("extract orig tarball when using export-dir option, "
                   "default is '%(overlay)s'"),
             'remote-url-pattern':
                  ("Remote url pattern to create the repo at, "
                   "default is '%(remote-url-pattern)s'"),
             'multimaint':
                  "Note multiple maintainers, default is '%(multimaint)s'",
             'multimaint-merge':
                  ("Merge commits by maintainer, "
                   "default is '%(multimaint-merge)s'"),
             'pbuilder':
                  ("Invoke git-pbuilder for building, "
                   "default is '%(pbuilder)s'"),
             'dist':
                  ("Build for this distribution when using git-pbuilder, "
                   "default is '%(dist)s'"),
             'arch':
                  ("Build for this architecture when using git-pbuilder, "
                   "default is '%(arch)s'"),
             'qemubuilder':
                  ("Invoke git-pbuilder with qemubuilder for building, "
                   "default is '%(qemubuilder)s'"),
             'interactive':
                  "Run command interactively, default is '%(interactive)s'",
             'color':
                  "Whether to use colored output, default is '%(color)s'",
             'color-scheme':
                  ("Colors to use in output (when color is enabled), format "
                   "is '<debug>:<info>:<warning>:<error>', e.g. "
                   "'cyan:34::'. Numerical values and color names are "
                   "accepted, empty fields indicate using the default."),
             'spawn-editor':
                  ("Whether to spawn an editor after adding the "
                   "changelog entry, default is '%(spawn-editor)s'"),
             'patch-numbers':
                  ("Whether to number patch files, "
                   "default is %(patch-numbers)s"),
             'patch-num-format':
                  ("The format specifier for patch number prefixes, "
                   "default is %(patch-num-format)s"),
             'renumber':
                  ("Whether to renumber patches exported from patch queues, "
                   "instead of preserving the number specified in "
                   "'Gbp-Pq: Name' tags, default is %(renumber)s"),
             'notify':
                  ("Whether to send a desktop notification after the build, "
                   "default is '%(notify)s'"),
             'merge':
                  ("After the import merge the result to the debian branch, "
                   "default is '%(merge)s'"),
             'merge-mode':
                  ("Howto merge the new upstream sources onto the debian branch"
                   "default is '%(merge-mode)s'"),
             'track':
                  ("Set up tracking for remote branches, "
                   "default is '%(track)s'"),
             'author-is-committer':
                  ("Use the authors's name also as the committer's name, "
                   "default is '%(author-is-committer)s'"),
             'author-date-is-committer-date':
                  ("Use the authors's date as the committer's date, "
                   "default is '%(author-date-is-committer-date)s'"),
             'create-missing-branches':
                  ("Create missing branches automatically, "
                   "default is '%(create-missing-branches)s'"),
             'submodules':
                  ("Transparently handle submodules in the upstream tree, "
                   "default is '%(submodules)s'"),
             'postimport':
                  ("hook run after a successful import, "
                   "default is '%(postimport)s'"),
             'build':
                  ("Enable running builder, default is %(build)s"),
             'hooks':
                  ("Enable running all hooks, default is %(hooks)s"),
             'time-machine':
                  ("don't try head commit only to apply the patch queue "
                   "but look TIME_MACHINE commits back, "
                   "default is '%(time-machine)d'"),
             'pbuilder-autoconf':
                  ("Wheter to configure pbuilder automatically, "
                   "default is '%(pbuilder-autoconf)s'"),
             'pbuilder-options':
                  ("Options to pass to pbuilder, "
                   "default is '%(pbuilder-options)s'"),
             'template-dir':
                  ("Template directory used by git init, "
                   "default is '%(template-dir)s'"),
             'remote-config':
                  ("Remote defintion in gbp.conf used to create the remote "
                   "repository, default is '%(remote-config)s'"),
             'allow-unauthenticated':
                  ("Don't verify integrity of downloaded source, "
                   "default is '%(allow-unauthenticated)s'"),
             'symlink-orig':
                  ("Whether to creat a symlink from the upstream tarball "
                   "to the orig.tar.gz if needed, default is "
                   "'%(symlink-orig)s'"),
              'purge':
                  "Purge exported package build directory. Default is '%(purge)s'",
              'drop':
                  ("In case of 'export' drop the patch-queue branch "
                   "after export. Default is '%(drop)s'"),
              'commit':
                  "commit changes after export, Default is '%(commit)s'",
              'tmp-dir':
                  ("Base directory under which temporary directories are "
                   "created, default is '%(tmp-dir)s'"),
           }

    def_config_files = {'/etc/git-buildpackage/gbp.conf': 'system',
                        '~/.gbp.conf':                    'global',
                        '%(top_dir)s/.gbp.conf':          None,
                        '%(top_dir)s/debian/gbp.conf':    'debian',
                        '%(git_dir)s/gbp.conf':           None}

    @classmethod
    def get_config_files(klass, no_local=False):
        """
        Get list of config files from the I{GBP_CONF_FILES} environment
        variable.

        @param no_local: don't return the per-repo configuration files
        @type no_local: C{bool}
        @return: list of config files we need to parse
        @rtype: C{list}

        >>> conf_backup = os.getenv('GBP_CONF_FILES')
        >>> if conf_backup is not None: del os.environ['GBP_CONF_FILES']
        >>> homedir = os.path.expanduser("~")
        >>> files = GbpOptionParser.get_config_files()
        >>> files_mangled = [file.replace(homedir, 'HOME') for file in files]
        >>> sorted(files_mangled)
        ['%(git_dir)s/gbp.conf', '%(top_dir)s/.gbp.conf', '%(top_dir)s/debian/gbp.conf', '/etc/git-buildpackage/gbp.conf', 'HOME/.gbp.conf']
        >>> files = GbpOptionParser.get_config_files(no_local=True)
        >>> files_mangled = [file.replace(homedir, 'HOME') for file in files]
        >>> sorted(files_mangled)
        ['/etc/git-buildpackage/gbp.conf', 'HOME/.gbp.conf']
        >>> os.environ['GBP_CONF_FILES'] = 'test1:test2'
        >>> GbpOptionParser.get_config_files()
        ['test1', 'test2']
        >>> del os.environ['GBP_CONF_FILES']
        >>> if conf_backup is not None: os.environ['GBP_CONF_FILES'] = conf_backup
        """
        envvar = os.environ.get('GBP_CONF_FILES')
        files = envvar.split(':') if envvar else klass.def_config_files.keys()
        files = [os.path.expanduser(fname) for fname in files]
        if no_local:
            files = [fname for fname in files if fname.startswith('/')]
        return files

    def _read_config_file(self, parser, repo, filename, git_treeish):
        """Read config file"""
        str_fields = {}
        if repo:
            str_fields['git_dir'] = repo.git_dir
            if not repo.bare:
                str_fields['top_dir'] = repo.path

        # Read per-tree config file
        if repo and git_treeish and filename.startswith('%(top_dir)s/'):
            with tempfile.TemporaryFile() as tmp:
                relpath = filename.replace('%(top_dir)s/', '')
                try:
                    config = repo.show('%s:%s' % (git_treeish, relpath))
                    tmp.writelines(config)
                except GitRepositoryError:
                    pass
                tmp.seek(0)
                parser.readfp(tmp)
                return
        try:
            filename = filename % str_fields
        except KeyError:
            # Skip if filename wasn't expanded, i.e. we're not in git repo
            return
        parser.read(filename)

    def _warn_old_config_section(self, oldcmd, cmd):
        if not os.getenv("GBP_DISABLE_SECTION_DEPRECTATION"):
            gbp.log.warn("Old style config section [%s] found "
                         "please rename to [%s]" % (oldcmd, cmd))

    def parse_config_files(self, git_treeish=None):
        """
        Parse the possible config files and set appropriate values
        default values
        """
        parser = configparser.SafeConfigParser()
        # Fill in the built in values
        self.config = dict(self.__class__.defaults)
        # Update with the values from the defaults section. This is needed
        # in case the config file doesn't have a [<command>] section at all
        config_files = self.get_config_files()
        try:
            repo = GitRepository(".")
        except GitRepositoryError:
            repo = None
        # Read all config files
        for filename in config_files:
            self._read_config_file(parser, repo, filename, git_treeish)
        self.config.update(dict(parser.defaults()))

        # Make sure we read any legacy sections prior to the real subcommands
        # section i.e. read [gbp-pull] prior to [pull]
        if (self.command.startswith('gbp-') or
            self.command.startswith('git-')):
            cmd = self.command[4:]
            oldcmd = self.command
            if parser.has_section(oldcmd):
                self.config.update(dict(parser.items(oldcmd, raw=True)))
                self._warn_old_config_section(oldcmd, cmd)
        else:
            cmd = self.command
            for prefix in ['gbp', 'git']:
                oldcmd = '%s-%s' % (prefix, self.command)
                if parser.has_section(oldcmd):
                    self.config.update(dict(parser.items(oldcmd, raw=True)))
                    self._warn_old_config_section(oldcmd, cmd)

        # Update with command specific settings
        if parser.has_section(cmd):
            # Don't use items() until we got rid of the compat sections
            # since this pulls in the defaults again
            self.config.update(dict(parser._sections[cmd].items()))

        for section in self.sections:
            if parser.has_section(section):
                self.config.update(dict(parser._sections[section].items()))
            else:
                raise configparser.NoSectionError(
                        "Mandatory section [%s] does not exist." % section)

        # filter can be either a list or a string, always build a list:
        if self.config['filter']:
            if self.config['filter'].startswith('['):
                self.config['filter'] = eval(self.config['filter'])
            else:
                self.config['filter'] = [ self.config['filter'] ]
        else:
            self.config['filter'] = []

    def __init__(self, command, prefix='', usage=None, sections=[],
                 git_treeish=None):
        """
        @param command: the command to build the config parser for
        @type command: C{str}
        @param prefix: A prefix to add to all command line options
        @type prefix: C{str}
        @param usage: a usage description
        @type usage: C{str}
        @param sections: additional (non optional) config file sections
            to parse
        @type sections: C{list} of C{str}
        """
        self.command = command
        self.sections = sections
        self.prefix = prefix
        self.config = {}
        self.parse_config_files(git_treeish)
        self.valid_options = []

        if self.command.startswith('git-') or self.command.startswith('gbp-'):
            prog = self.command
        else:
            prog = "gbp %s" % self.command
        OptionParser.__init__(self, option_class=GbpOption,
                              prog=prog,
                              usage=usage, version='%s %s' % (self.command,
                                                              gbp_version))

    def _is_boolean(self, dummy, *unused, **kwargs):
        """is option_name a boolean option"""
        ret = False
        try:
            if kwargs['action'] in [ 'store_true', 'store_false' ]:
                ret=True
        except KeyError:
            ret=False
        return ret

    def _get_bool_default(self, option_name):
        """
        get default for boolean options
        this way we can handle no-foo=True and foo=False
        """
        if option_name.startswith('no-'):
            pos = option_name[3:]
            neg = option_name
        else:
            pos = option_name
            neg = "no-%s" % option_name

        try:
            default = self.config[pos]
        except KeyError:
            default = self.config[neg]

        if default.lower() in ["true",  "1" ]:
            val = 'True'
        elif default.lower() in ["false", "0" ]:
            val = 'False'
        else:
            raise ValueError("Boolean options must be True or False")
        return eval(val)

    def get_default(self, option_name, **kwargs):
        """get the default value"""
        if self._is_boolean(self, option_name, **kwargs):
            default = self._get_bool_default(option_name)
        else:
            default = self.config[option_name]
        return default

    @safe_option
    def add_config_file_option(self, option_name, help=None, **kwargs):
        """
        set a option for the command line parser, the default is read from the config file
        param option_name: name of the option
        type option_name: string
        param help: help text
        type help: string
        """
        if not help:
            help = self.help[option_name]
        OptionParser.add_option(self, "--%s%s" % (self.prefix, option_name),
                                default=self.get_default(option_name, **kwargs),
                                help=help % self.config, **kwargs)

    def add_boolean_config_file_option(self, option_name, dest=None):
        self.add_config_file_option(option_name=option_name, dest=dest, action="store_true")
        neg_help = "negates '--%s%s'" % (self.prefix, option_name)
        self.add_config_file_option(option_name="no-%s" % option_name, dest=dest, help=neg_help, action="store_false")

    def get_config_file_value(self, option_name):
        """
        Query a single interpolated config file value.

        @param option_name: the config file option to look up
        @type option_name: string
        @returns: The config file option value or C{None} if it doesn't exist
        @rtype: C{str} or C{None}
        """
        return self.config.get(option_name)

    def print_help(self, file=None):
        """
        Print an extended help message, listing all options and any
        help text provided with them, to 'file' (default stdout).
        """
        if file is None:
            file = sys.stdout
        encoding = self._get_encoding(file)
        try:
            file.write(self.format_help().encode(encoding, "replace"))
        except IOError as e:
            if e.errno != errno.EPIPE:
                raise

    @classmethod
    def _name_to_filename(cls, name):
        """
        Translate a name like 'system' to a config file name

        >>> GbpOptionParser._name_to_filename('foo')
        >>> GbpOptionParser._name_to_filename('system')
        '/etc/git-buildpackage/gbp.conf'
        >>> GbpOptionParser._name_to_filename('global')
        '~/.gbp.conf'
        >>> GbpOptionParser._name_to_filename('debian')
        '%(top_dir)s/debian/gbp.conf'
        """
        for k, v in cls.def_config_files.items():
            if name == v:
                return k
        else:
            return None

    @classmethod
    def _set_config_file_value(cls, section, option, value, name=None, filename=None):
        """
        Write a config value to a file creating it if needed

        On errors a ConfigParserError is raised
        """
        if not name and not filename:
            raise configparser.Error("Either 'name' or 'filename' must be given")
        if not filename:
            filename = os.path.expanduser(cls._name_to_filename(name))

        # Create e new config parser since we only operate on a single file
        cfg = configparser.RawConfigParser()
        cfg.read(filename)
        if not cfg.has_section(section):
            cfg.add_section(section)
        cfg.set(section, option, value)
        with open(filename, 'w') as fp:
            cfg.write(fp)


class GbpOptionGroup(OptionGroup):
    @safe_option
    def add_config_file_option(self, option_name, dest, help=None, **kwargs):
        """
        set a option for the command line parser, the default is read from the config file
        param option_name: name of the option
        type option_name: string
        param dest: where to store this option
        type dest: string
        param help: help text
        type help: string
        """
        if not help:
            help = self.parser.help[option_name]
        OptionGroup.add_option(self, "--%s%s" % (self.parser.prefix, option_name), dest=dest,
                                default=self.parser.get_default(option_name, **kwargs),
                                help=help % self.parser.config, **kwargs)

    def add_boolean_config_file_option(self, option_name, dest):
        self.add_config_file_option(option_name=option_name, dest=dest, action="store_true")
        neg_help = "negates '--%s%s'" % (self.parser.prefix, option_name)
        self.add_config_file_option(option_name="no-%s" % option_name, dest=dest, help=neg_help, action="store_false")


class GbpOptionParserDebian(GbpOptionParser):
    """
    Handles commandline options and parsing of config files for Debian tools
    """
    defaults = dict(GbpOptionParser.defaults)
    defaults.update( {
                       'builder'            : 'debuild -i -I',
                     } )


class GbpOptionParserRpm(GbpOptionParser):
    """
    Handles commandline options and parsing of config files for rpm tools
    """
    defaults = dict(GbpOptionParser.defaults)
    defaults.update({
            'vendor'                    : 'Downstream',
            'packaging-branch'          : 'master',
            'packaging-dir'             : '',
            'packaging-tag-msg'         : ('%(pkg)s (vendor)s release '
                                                         '%(version)s'),
            'packaging-tag'             : 'packaging/%(version)s',
            'pq-branch'                 : 'development/%(branch)s',
            'export-sourcedir'          : 'SOURCES',
            'export-specdir'            : 'SPECS',
            'export-dir'                : '../rpmbuild',
            'builder'                   : 'rpmbuild',
            'spec-file'                 : '',
            'mock'                      : 'False',
            'dist'                      : '',
            'arch'                      : '',
            'mock-root'                 : '',
            'mock-options'              : '',
            'native'                    : 'auto',
            'spec-vcs-tag'              : '',
            'patch-export'              : 'False',
            'patch-compress'            : '0',
            'patch-squash'              : '',
            'patch-ignore-path'         : '',
            'patch-import'              : 'True',
            'import-files'              : ['.gbp.conf',
                                           'debian/gbp.conf'],
            'merge'                     : 'False',
            'pristine-tarball-name'     : 'auto',
            'orig-prefix'               : 'auto',
            'changelog-file'            : 'auto',
            'changelog-revision'        : '',
            'spawn-editor'              : 'always',
            'editor-cmd'                : 'vim',
            'meta-bts'                  : '(Close|Closes|Fixes|Fix)',
                    })

    help = dict(GbpOptionParser.help)
    help.update({
            'vendor':
                "Distribution vendor name, default is '%(vendor)s'",
            'packaging-branch':
                "Branch the packaging is being maintained on, rpm counterpart "
                "of the 'debian-branch' option, default is "
                "'%(packaging-branch)s'",
            'packaging-dir':
                "Subdir for RPM packaging files, default is "
                "'%(packaging-dir)s'",
            'packaging-tag':
                "Format string for packaging tags, RPM counterpart of the "
                "'debian-tag' option, default is '%(packaging-tag)s'",
            'packaging-tag-msg':
               ("Format string for packaging tag messages, "
                "default is '%(packaging-tag-msg)s'"),
            'pq-branch':
                "format string for the patch-queue branch name, default is "
                "'%(pq-branch)s'",
            'spec-file':
                "Spec file to use, causes the packaging-dir option to be "
                "ignored, default is '%(spec-file)s'",
            'export-sourcedir':
                "Subdir (under EXPORT_DIR) where packaging sources (other than "
                "the spec file) are exported, default is "
                "'%(export-sourcedir)s'",
            'export-specdir':
                "Subdir (under EXPORT_DIR) where package spec file is "
                "exported default is '%(export-specdir)s'",
             'mock':
                  ("Invoke mock for building using gbp-builder-mock, "
                   "default is '%(mock)s'"),
             'dist':
                 ("Build for this distribution when using mock. E.g.: epel-6, "
                   "default is '%(dist)s'"),
             'arch':
                  ("Build for this architecture when using mock, "
                   "default is '%(arch)s'"),
             'mock-root':
                 ("The mock root (-r) name for building with mock: <dist>-<arch>, "
                   "default is '%(mock-root)s'"),
             'mock-options':
                  ("Options to pass to mock, "
                   "default is '%(mock-options)s'"),
            'native':
                "Treat this package as native, default is '%(native)s'",
            'spec-vcs-tag':
                "Set/update the 'VCS:' tag in the spec file, empty value "
                "removes the tag entirely, default is '%(spec-vcs-tag)s'",
            'patch-export':
                "Create patches between upstream and export-treeish, default "
                "is '%(patch-export)s'",
            'patch-compress':
                "Compress (auto-generated) patches larger than given number of "
                "bytes, 0 never compresses, default is "
                "'%(patch-compress)s'",
            'patch-squash':
                "Squash commits (from upstream) until given tree-ish into one "
                "big diff, format is '<commit_ish>[:<filename_base>]'. "
                "Default is '%(patch-squash)s'",
            'patch-ignore-path':
                "Exclude changes to path(s) matching regex, default is "
                "'%(patch-ignore-path)s'",
            'patch-import':
                "Import patches to the packaging branch, default is "
                "'%(patch-import)s'",
            'import-files':
                "Comma-separated list of additional file(s) to import from "
                "packaging branch. These will appear as one monolithic patch "
                "in the pq/development branch. Default is %(import-files)s",
            'pristine-tarball-name':
                "Filename to record to pristine-tar, set to 'auto' to not "
                "mangle the file name, default is '%(pristine-tarball-name)s'",
            'orig-prefix':
                "Prefix (dir) to be used when generating/importing tarballs, "
                "default is '%(orig-prefix)s'",
            'changelog-file':
                "Changelog file to be used, default is '%(changelog-file)s'",
            'changelog-revision':
                "Format string for the revision field in the changelog header. "
                "If empty or not defined the default from packaging policy is "
                "used.",
            'editor-cmd':
                "Editor command to use",
            'git-author':
                "Use name and email from git-config for the changelog header, "
                "default is '%(git-author)s'",
            'meta-bts':
                "Meta tags for the bts commands, default is '%(meta-bts)s'",
                 })

class GbpOptionParserBB(GbpOptionParserRpm):
    """Commandline and config file option parser for the -bb commands"""
    defaults = dict(GbpOptionParserRpm.defaults)
    defaults.update( {
                       'builder'                : 'bitbake',
                       'export-dir'             : '',
                       'meta-dir'               : '',
                       'bb-file'                : '',
                       'bb-vcs-info'            : '',
                       'submit-tag'             : 'submit/%(target)s/%(nowtime)s',
                       'target'                 : 'tizen',
                     } )

    help = dict(GbpOptionParserRpm.help)
    help.update( {
                   'meta-dir':
                        "Subdir where bitbake meta files are stored, default "
                        "is '%(meta-dir)s'",
                   'bb-file':
                        "Bitbake recipe file to build",
                   'bb-vcs-info':
                        "Format string for the VCS information automatically "
                        "set in the recipe file, default is '%(bb-vcs-info)s'",
                   'submit-tag':
                        "Submit tag format, default is '%(submit-tag)s'",
                   'target':
                        "Submit target used in submit tag, default is "
                        "'%(target)s'",
                 } )

# vim:et:ts=4:sw=4:et:sts=4:ai:set list listchars=tab\:»·,trail\:·:

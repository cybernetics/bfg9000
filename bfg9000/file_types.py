import copy as _copy
from six import iteritems as _iteritems, with_metaclass as _with_metaclass

try:
    from enum import EnumMeta as _EnumMeta, Flag as _Flag
except ImportError:
    from enum import EnumMeta as _EnumMeta, IntEnum as _Flag

from .iterutils import listify as _listify
from .languages import src2lang as _src2lang, hdr2lang as _hdr2lang
from .options import option_list as _option_list
from .path import InstallRoot as _InstallRoot, install_path as _install_path
from .safe_str import safe_str as _safe_str


def installify(file, *args, **kwargs):
    file = _copy.copy(file)
    file.path = _install_path(
        file.path, file.install_root, directory=isinstance(file, Directory),
        *args, **kwargs
    )
    return file


class Node(object):
    private = False

    def __init__(self, path):
        self.creator = None
        self.path = path

    def _safe_str(self):
        return _safe_str(self.path)

    @property
    def all(self):
        return [self]

    def __repr__(self):
        return '<{type} {name}>'.format(
            type=type(self).__name__, name=repr(self.path)
        )

    def __hash__(self):
        return hash(self.path)

    def __eq__(self, rhs):
        return type(self) == type(rhs) and self.path == rhs.path


class Phony(Node):
    pass


class File(Node):
    install_kind = None
    install_root = None

    def __init__(self, path, external=False):
        Node.__init__(self, path)
        self.external = external
        self.post_install = None

    @property
    def install_deps(self):
        return []


class Directory(File):
    def __init__(self, path, files=None, external=False):
        File.__init__(self, path, external)
        self.files = files


class SourceFile(File):
    def __init__(self, path, lang=None, external=False):
        File.__init__(self, path, external)
        self.lang = lang or _src2lang.get(path.ext())


class HeaderFile(File):
    install_kind = 'data'
    install_root = _InstallRoot.includedir

    def __init__(self, path, lang=None, external=False):
        File.__init__(self, path, external)
        self.lang = lang or _hdr2lang.get(path.ext())


class PrecompiledHeader(HeaderFile):
    install_kind = None


class MsvcPrecompiledHeader(PrecompiledHeader):
    def __init__(self, path, object_path, header_name, format, lang=None,
                 external=False):
        PrecompiledHeader.__init__(self, path, lang, external)
        self.object_file = ObjectFile(object_path, format, self.lang, external)
        self.object_file.private = True
        self.header_name = header_name


class HeaderDirectory(Directory):
    install_kind = 'data'
    install_root = _InstallRoot.includedir

    def __init__(self, path, files=None, system=False, langs=None,
                 external=False):
        Directory.__init__(self, path, files, external)
        self.system = system
        self.langs = _listify(langs)


class Binary(File):
    install_kind = 'program'
    install_root = _InstallRoot.libdir

    def __init__(self, path, format, lang=None, external=False):
        File.__init__(self, path, external)
        self.format = format
        self.lang = lang


class ObjectFile(Binary):
    pass


# This is used by JVM languages to hold a list of all the object files
# generated by a particular source file's compilation.
class ObjectFileList(ObjectFile):
    def __init__(self, path, object_name, format, lang=None, external=False):
        ObjectFile.__init__(self, path, format, lang, external)
        self.object_file = ObjectFile(object_name, format, lang, external)


# This is sort of a misnomer. It's really just "a binary that is not an object
# file", even though it's not necessarily been linked.
class LinkedBinary(Binary):
    def __init__(self, *args, **kwargs):
        Binary.__init__(self, *args, **kwargs)
        self.runtime_deps = []
        self.linktime_deps = []
        self.package_deps = []

    @property
    def install_deps(self):
        return self.runtime_deps + self.linktime_deps


class Executable(LinkedBinary):
    install_root = _InstallRoot.bindir


class Library(LinkedBinary):
    @property
    def runtime_file(self):
        return None


# This is used for JVM binaries, which can be both executables and libraries.
# Multiple inheritance is a sign that we should perhaps switch to a trait-based
# system though...
class ExecutableLibrary(Executable, Library):
    install_root = _InstallRoot.libdir


class SharedLibrary(Library):
    @property
    def runtime_file(self):
        return self


class LinkLibrary(SharedLibrary):
    def __init__(self, path, library, external=False):
        SharedLibrary.__init__(self, path, library.format, library.lang,
                               external)
        self.library = library
        self.linktime_deps = [library]

    @property
    def runtime_file(self):
        return self.library


class VersionedSharedLibrary(SharedLibrary):
    def __init__(self, path, format, lang, soname, linkname, external=False):
        SharedLibrary.__init__(self, path, format, lang, external)
        self.soname = LinkLibrary(soname, self, external)
        self.link = LinkLibrary(linkname, self.soname, external)


class StaticLibrary(Library):
    def __init__(self, *args, **kwargs):
        Library.__init__(self, *args, **kwargs)
        self.forward_opts = {}


class WholeArchive(StaticLibrary):
    def __init__(self, library):
        self.library = library

    def __getattribute__(self, name):
        if name in ['library', '_safe_str', '__repr__', '__hash__', '__eq__']:
            return object.__getattribute__(self, name)
        return getattr(object.__getattribute__(self, 'library'), name)


class ExportFile(File):
    private = True


# This refers specifically to DLL files that have an import library, not just
# anything with a .dll extension (for instance, .NET DLLs are just regular
# shared libraries. While this is a "library" in some senses, since you can't
# link to it during building, we just consider it a LinkedBinary.
class DllBinary(LinkedBinary):
    install_root = _InstallRoot.bindir
    private = True

    def __init__(self, path, format, lang, import_name, export_name=None,
                 external=False):
        LinkedBinary.__init__(self, path, format, lang, external)
        self.import_lib = LinkLibrary(import_name, self, external)
        self.export_file = ExportFile(export_name, external)


class DualUseLibrary(object):
    def __init__(self, shared, static):
        self.shared = shared
        self.static = static
        self.shared.parent = self
        self.static.parent = self

    @property
    def all(self):
        return [self.shared, self.static]

    def __repr__(self):
        return '<DualUseLibrary {!r}>'.format(self.shared.path)

    def __hash__(self):
        return hash(self.shared.path)

    def __eq__(self, rhs):
        return (type(self) == type(rhs) and
                self.shared.path == rhs.shared.path and
                self.static.path == rhs.static.path)

    @property
    def package_deps(self):
        return self.shared.package_deps

    @property
    def install_deps(self):
        return self.shared.install_deps

    @property
    def forward_opts(self):
        return self.static.forward_opts


class PkgConfigPcFile(File):
    install_root = _InstallRoot.libdir


# Package-related objects; these aren't files in the same sense as those listed
# above, but they're very similar.


class _PackageKindMeta(_EnumMeta):
    def __getitem__(cls, name):
        try:
            return _EnumMeta.__getitem__(cls, name)
        except KeyError:
            pass
        raise ValueError('kind must be one of: {}'.format(', '.join(
            "'{}'".format(i.name) for i in cls
        )))


class PackageKind(_with_metaclass(_PackageKindMeta, _Flag)):
        static = 1
        shared = 2
        any = static | shared


class Package(object):
    is_package = True

    def __init__(self, name, format):
        self.name = name
        self.format = format

    def __hash__(self):
        return hash(self.name)

    def __eq__(self, rhs):
        return type(self) == type(rhs) and self.name == rhs.name

    def __repr__(self):
        return '<{type} {name}>'.format(
            type=type(self).__name__, name=repr(self.name)
        )


class CommonPackage(Package):
    def __init__(self, name, format, compile_options=None, link_options=None):
        Package.__init__(self, name, format)
        self._compile_options = compile_options or _option_list()
        self._link_options = link_options or _option_list()

    def compile_options(self, compiler, output):
        if hasattr(compiler, 'options'):
            return self._compile_options + compiler.options(output, self)
        return self._compile_options

    def link_options(self, linker, output):
        if hasattr(linker, 'options'):
            return self._link_options + linker.options(output, self)
        return self._link_options


# A reference to a macOS framework. Can be used in place of Library objects.
class Framework(object):
    def __init__(self, name, suffix=None):
        self.name = name
        self.suffix = suffix

    @property
    def full_name(self):
        return self.name + ',' + self.suffix if self.suffix else self.name

    @property
    def runtime_file(self):
        None

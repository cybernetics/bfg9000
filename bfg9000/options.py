from . import iterutils, safe_str


class option_list(object):
    def __init__(self, *args):
        self._options = []
        self.collect(*args)

    def append(self, option):
        if ( isinstance(option, safe_str.stringy_types) or
             not any(option.matches(i) for i in self._options) ):
            self._options.append(option)

    def extend(self, options):
        for i in options:
            self.append(i)

    def collect(self, *args):
        for i in args:
            if iterutils.isiterable(i):
                for j in i:
                    self.collect(j)
            elif i is not None:
                self.append(i)

    def copy(self):
        return option_list(self._options)

    def __iter__(self):
        return iter(self._options)

    def __len__(self):
        return len(self._options)

    def __eq__(self, rhs):
        return type(self) == type(rhs) and self._options == rhs._options

    def __repr__(self):
        return '<option_list({})>'.format(repr(self._options))

    def __add__(self, rhs):
        x = self.copy()
        x += rhs
        return x

    def __iadd__(self, rhs):
        if not isinstance(rhs, option_list):
            raise TypeError('expected an option_list, got a {!r}'
                            .format(type(rhs)))
        self._options.extend(rhs._options)
        return self


class Option(object):
    __slots__ = ()

    def __init__(self, *args):
        if len(args) != len(self.__slots__):
            raise TypeError('__init__() takes exactly {} arguments ({} given)'
                            .format(len(self.__slots__) + 1, len(args) + 1))
        for k, v in zip(self.__slots__, args):
            setattr(self, k, v)

    def matches(self, rhs):
        return self == rhs

    def __eq__(self, rhs):
        return type(self) == type(rhs) and all(
            getattr(self, i) == getattr(rhs, i) for i in self.__slots__
        )

    def __repr__(self):
        return '<{}({})>'.format(self.__class__.__name__, ','.join(
            repr(getattr(self, i)) for i in self.__slots__
        ))


def option(name, attrs=()):
    return type(name, (Option,), {'__slots__': attrs})


pthread = option('pthread')
pic = option('pic')
define = option('define', ('name',))
include_dir = option('include_dir', ('directory',))
pch = option('pch', ('header',))
lib_dir = option('lib_dir', ('directory',))
lib = option('lib', ('library',))
rpath_dir = option('rpath_dir', ('path',))
rpath_link_dir = option('rpath_link_dir', ('path',))
lib_literal = option('lib_literal', ('value',))
entry_point = option('entry_point', ('value',))
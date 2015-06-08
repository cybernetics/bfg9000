import operator
import os
import re
from cStringIO import StringIO
from collections import namedtuple, OrderedDict
from itertools import chain

from .. import path
from .. import safe_str
from .. import shell
from .. import utils

_rule_handlers = {}
def rule_handler(rule_name):
    def decorator(fn):
        _rule_handlers[rule_name] = fn
        return fn
    return decorator

MakeInclude = namedtuple('MakeInclude', ['name', 'optional'])
MakeRule = namedtuple('MakeRule', ['targets', 'deps', 'order_only', 'recipe',
                                   'variables', 'phony'])

class Pattern(object):
    def __init__(self, path):
        if len(re.findall(r'([^\\]|^)(\\\\)*%', path)) != 1:
            raise ValueError('exactly one % required')
        self.path = path

    def use(self):
        bits = re.split(r'%', self.path)
        delim = safe_str.escaped_str('%')
        return reduce(operator.add, utils.tween(bits, delim, flag=False))

    def _safe_str(self):
        return self.use()

    def __str__(self):
        raise NotImplementedError()

    def __repr__(self):
        return repr(self.use())

    def __hash__(self):
        return hash(self.path)

    def __cmp__(self, rhs):
        return cmp(self.path, rhs.path)

    def __add__(self, rhs):
        return self.use() + rhs

    def __radd__(self, lhs):
        return lhs + self.use()

class MakeVariable(object):
    def __init__(self, name):
        self.name = re.sub(r'[\s:#=]', '_', name)

    def use(self):
        fmt = '${}' if len(self.name) == 1 else '$({})'
        return safe_str.escaped_str(fmt.format(self.name))

    def _safe_str(self):
        return self.use()

    def __str__(self):
        raise NotImplementedError()

    def __repr__(self):
        return repr(self.use())

    def __hash__(self):
        return hash(self.name)

    def __cmp__(self, rhs):
        return cmp(self.name, rhs.name)

    def __add__(self, rhs):
        return self.use() + rhs

    def __radd__(self, lhs):
        return lhs + self.use()

var = MakeVariable

class MakeFunc(object):
    def __init__(self, name, *args):
        self.name = name
        self.args = args

    def use(self):
        out = StringIO()
        MakeWriter._write_literal(out, '$(' + self.name + ' ')
        for tween, i in utils.tween(self.args, ','):
            if tween:
                MakeWriter._write_literal(out, i)
            else:
                MakeWriter._write_each(out, utils.iterate(i), syntax='function')
        MakeWriter._write_literal(out, ')')
        return safe_str.escaped_str(out.getvalue())

    def _safe_str(self):
        return self.use()

    def __str__(self):
        raise NotImplementedError()

    def __repr__(self):
        return repr(self.use())

class MakeCall(MakeFunc):
    def __init__(self, func, *args):
        if not isinstance(func, MakeVariable):
            func = MakeVariable(func)
        MakeFunc.__init__(self, 'call', func.name, *args)

_path_vars = {
    'srcdir': MakeVariable('srcdir'),
    'prefix': MakeVariable('prefix'),
}
class MakeWriter(object):
    def __init__(self):
        # TODO: Sort variables in some useful order.
        self._global_variables = OrderedDict()
        self._target_variables = OrderedDict({Pattern('%'): OrderedDict()})
        self._defines = OrderedDict()

        self._rules = []
        self._targets = set()
        self._includes = []

        # Necessary for escaping commas in function calls.
        self.variable(',', ',')

    def variable(self, name, value, flavor='simple', target=None):
        if not isinstance(name, MakeVariable):
            name = MakeVariable(name)
        if self.has_variable(name, target=target):
            raise ValueError('variable "{}" already exists'.format(name))

        if flavor == 'define':
            self._defines[name] = value
        else:
            if target:
                if not target in self._target_variables:
                    self._target_variables[target] = OrderedDict()
                self._target_variables[target][name] = (value, flavor)
            else:
                self._global_variables[name] = (value, flavor)
        return name

    def has_variable(self, name, target=None):
        if not isinstance(name, MakeVariable):
            name = MakeVariable(name)
        if target:
            return (target in self._target_variables and
                    name in self._target_variables[target])
        else:
            return (name in self._global_variables or
                    name in self._defines)

    def include(self, name, optional=False):
        self._includes.append(MakeInclude(name, optional))

    def rule(self, target, deps=None, order_only=None, recipe=None,
             variables=None, phony=False):
        real_variables = {}
        if variables:
            for k, v in variables.iteritems():
                if not isinstance(k, MakeVariable):
                    k = MakeVariable(k)
                real_variables[k] = v

        targets = utils.listify(target)
        if len(targets) == 0:
            raise ValueError('must have at least one target')
        for i in targets:
            if self.has_rule(i):
                raise ValueError('rule for "{}" already exists'.format(i))
            self._targets.add(i)
        self._rules.append(MakeRule(
            targets, utils.listify(deps), utils.listify(order_only), recipe,
            real_variables, phony
        ))

    def has_rule(self, name):
        return name in self._targets

    @classmethod
    def escape_str(cls, string, syntax):
        def repl(match):
            return match.group(1) * 2 + '\\' + match.group(2)
        result = string.replace('$', '$$')

        if syntax == 'target':
            return re.sub(r'(\\*)([#?*\[\]~\s%])', repl, result)
        elif syntax == 'dependency':
            return re.sub(r'(\\*)([#?*\[\]~\s|%])', repl, result)
        elif syntax == 'shell_line':
            return result
        elif syntax == 'shell_word':
            return shell.quote(result)
        elif syntax == 'function':
            return shell.quote(re.sub(',', '$,', result))
        else:
            raise ValueError('unknown syntax "{}"'.format(syntax))

    @classmethod
    def _write_literal(cls, out, string):
        out.write(string)

    @classmethod
    def _write(cls, out, thing, syntax):
        thing = safe_str.safe_str(thing)

        if isinstance(thing, basestring):
            cls._write_literal(out, cls.escape_str(thing, syntax))
        elif isinstance(thing, safe_str.escaped_str):
            cls._write_literal(out, thing.string)
        elif isinstance(thing, path.real_path):
            if thing.base != 'builddir':
                cls._write(out, _path_vars[thing.base], syntax)
                cls._write_literal(out, os.sep)
            cls._write(out, thing.path, syntax)
        elif isinstance(thing, safe_str.jbos):
            for j in thing.bits:
                cls._write(out, j, syntax)
        else:
            raise TypeError(type(thing))

    @classmethod
    def _write_each(cls, out, things, syntax, delim=' ', prefix=None,
                    suffix=None):
        for tween, i in utils.tween(things, delim, prefix, suffix):
            cls._write_literal(out, i) if tween else cls._write(out, i, syntax)

    @classmethod
    def _write_shell(cls, out, thing):
        if utils.isiterable(thing):
            cls._write_each(out, thing, 'shell_word')
        else:
            cls._write(out, thing, 'shell_line')

    def _write_variable(self, out, name, value, flavor, target=None):
        operator = ' := ' if flavor == 'simple' else ' = '
        if target:
            self._write(out, target, syntax='target')
            self._write_literal(out, ': ')
        self._write_literal(out, name.name + operator)
        self._write_shell(out, value)
        self._write_literal(out, '\n')

    def _write_define(self, out, name, value):
        self._write_literal(out, 'define ' + name.name + '\n')
        for line in value:
            self._write_shell(out, line)
            self._write_literal(out, '\n')
        self._write_literal(out, 'endef\n\n')

    def _write_rule(self, out, rule):
        if rule.variables:
            for target in rule.targets:
                for name, value in rule.variables.iteritems():
                    self._write_variable(out, name, value, 'simple',
                                         target=target)

        if rule.phony:
            self._write_literal(out, '.PHONY: ')
            self._write_each(out, rule.targets, syntax='dependency')
            self._write_literal(out, '\n')

        self._write_each(out, rule.targets, syntax='target')
        self._write_literal(out, ':')
        self._write_each(out, rule.deps, syntax='dependency', prefix=' ')
        self._write_each(out, rule.order_only, syntax='dependency',
                         prefix=' | ')

        if (isinstance(rule.recipe, MakeVariable) or
            isinstance(rule.recipe, MakeFunc)):
            self._write_literal(out, ' ; ')
            self._write(out, rule.recipe, syntax='shell_line')
        elif rule.recipe is not None:
            for cmd in rule.recipe:
                self._write_literal(out, '\n\t')
                self._write_shell(out, cmd)
        self._write_literal(out, '\n\n')

    def write(self, out):
        # Don't let make use built-in suffix rules.
        self._write_literal(out, '.SUFFIXES:\n\n')

        for name, value in self._global_variables.iteritems():
            self._write_variable(out, name, value[0], value[1])
        if self._global_variables:
            self._write_literal(out, '\n')

        newline = False
        for target, each in self._target_variables.iteritems():
            for name, value in each.iteritems():
                newline = True
                self._write_variable(out, name, value[0], value[1], target)
        if newline:
            self._write_literal(out, '\n')

        for name, value in self._defines.iteritems():
            self._write_define(out, name, value)

        for r in self._rules:
            self._write_rule(out, r)

        for i in self._includes:
            self._write_literal(out, ('-' if i.optional else '') + 'include ')
            self._write(out, i.name, syntax='target')
            self._write_literal(out, '\n')

def write(env, build_inputs):
    writer = MakeWriter()
    writer.variable(_path_vars['srcdir'], env.srcdir)

    all_rule(build_inputs.get_default_targets(), writer)
    install_rule(build_inputs.install_targets, writer, env)
    for e in build_inputs.edges:
        _rule_handlers[type(e).__name__](e, build_inputs, writer)
    directory_rule(writer)
    regenerate_rule(writer, env)

    with open(os.path.join(env.builddir, 'Makefile'), 'w') as out:
        writer.write(out)

def cmd_var(compiler, writer):
    var = MakeVariable(compiler.command_var.upper())
    if not writer.has_variable(var):
        writer.variable(var, compiler.command_name)
    return var

def flags_vars(name, value, writer):
    name = name.upper()
    global_flags = MakeVariable('GLOBAL_{}'.format(name))
    if not writer.has_variable(global_flags):
        writer.variable(global_flags, value)

    flags = MakeVariable(name)
    if not writer.has_variable(flags, target=Pattern('%')):
        writer.variable(flags, global_flags, target=Pattern('%'))

    return global_flags, flags

def all_rule(default_targets, writer):
    writer.rule(
        target='all',
        deps=[i.path for i in default_targets],
        phony=True
    )

# TODO: Write a better `install` program to simplify this
def install_rule(install_targets, writer, env):
    if not install_targets:
        return

    writer.variable(_path_vars['prefix'], env.install_prefix)

    def install_cmd(kind):
        install = MakeVariable('INSTALL')
        if not writer.has_variable(install):
            writer.variable(install, 'install')

        if kind == 'program':
            install_program = MakeVariable('INSTALL_PROGRAM')
            if not writer.has_variable(install_program):
                writer.variable(install_program, install)
            return install_program
        else:
            install_data = MakeVariable('INSTALL_DATA')
            if not writer.has_variable(install_data):
                writer.variable(install_data, [install, '-m', '644'])
            return install_data

    def install_line(file):
        src = file.path.local_path()
        dst = file.path.install_path()
        return [install_cmd(file.install_kind), '-D', src, dst]

    def mkdir_line(dir):
        src = dir.path.append('*').local_path()
        dst = dir.path.parent().install_path()
        return 'mkdir -p ' + dst + ' && cp -r ' + src + ' ' + dst

    recipe = ([install_line(i) for i in install_targets.files] +
              [mkdir_line(i) for i in install_targets.directories])
    writer.rule(
        target='install',
        deps='all',
        recipe=recipe,
        phony=True
    )

dir_sentinel = '.dir'
def directory_rule(writer):
    # XXX: `mkdir -p` isn't safe (or valid!) on all platforms.
    writer.rule(
        target=Pattern(os.path.join('%', dir_sentinel)),
        recipe=[
            ['@mkdir', '-p', MakeFunc('dir', var('@'))],
            ['@touch', var('@')]
        ]
    )

def regenerate_rule(writer, env):
    writer.rule(
        target=path.Path('Makefile', path.Path.builddir, path.Path.basedir),
        deps=path.Path('build.bfg', path.Path.srcdir, path.Path.basedir),
        recipe=[[env.bfgpath, '--regenerate', '.']]
    )

@rule_handler('Compile')
def emit_object_file(rule, build_inputs, writer):
    compiler = rule.builder
    recipename = MakeVariable('RULE_{}'.format(compiler.name.upper()))
    global_cflags, cflags = flags_vars(
        compiler.command_var + 'FLAGS',
        compiler.global_args +
          build_inputs.global_options.get(rule.file.lang, []),
        writer
    )

    path = rule.target.path
    target_dir = path.parent()

    variables = {}
    cflags_value = []

    if rule.in_shared_library:
        cflags_value.extend(compiler.library_args)
    cflags_value.extend(chain.from_iterable(
        compiler.include_dir(i) for i in rule.include
    ))
    cflags_value.extend(rule.options)
    if cflags_value:
        variables[cflags] = [global_cflags] + cflags_value

    if not writer.has_variable(recipename):
        command_kwargs = {}
        recipe_extra = []
        if compiler.deps_flavor == 'gcc':
            command_kwargs['deps'] = deps = var('@') + '.d'
            # Munge the depfile so that it works a little better. See
            # <http://scottmcpeak.com/autodepend/autodepend.html> for a
            # discussion of how this works.
            recipe_extra = [
                r"@sed -e 's/.*://' -e 's/\\$//' < " + deps + ' | fmt -1 | \\',
                "  sed -e 's/^ *//' -e 's/$/:/' >> " + deps
            ]
            writer.include(path.addext('.d'), optional=True)
        elif compiler.deps_flavor == 'msvc':
            command_kwargs['deps'] = True

        writer.variable(recipename, [
            compiler.command(
                cmd=cmd_var(compiler, writer), input=var('<'), output=var('@'),
                args=cflags, **command_kwargs
            ),
        ] + recipe_extra, flavor='define')

    writer.rule(
        target=path,
        deps=[i.path for i in chain([rule.file], rule.extra_deps)],
        order_only=[target_dir.append(dir_sentinel)] if target_dir else None,
        recipe=recipename,
        variables=variables
    )

@rule_handler('Link')
def emit_link(rule, build_inputs, writer):
    linker = rule.builder
    recipename = MakeVariable('RULE_{}'.format(linker.name.upper()))
    global_ldflags, ldflags = flags_vars(
        linker.link_var + 'FLAGS', linker.global_args, writer
    )

    # TODO: Handle rules with multiple targets (e.g. shared libs on Windows).
    path = rule.target.path
    target_dir = path.parent()

    variables = {}
    command_kwargs = {}
    ldflags_value = list(linker.mode_args)
    lib_deps = [i for i in rule.libs if i.creator]

    if linker.mode != 'static_library':
        ldflags_value.extend(rule.options)
        ldflags_value.extend(linker.lib_dirs(lib_deps))

        target_dirname = target_dir.local_path().path
        ldflags_value.extend(linker.rpath(
            # TODO: Provide a relpath function for Path objects?
            os.path.relpath(i.path.parent().local_path().path, target_dirname)
            for i in lib_deps
        ))

        global_ldlibs, ldlibs = flags_vars(
            linker.link_var + 'LIBS', linker.global_libs, writer
        )
        command_kwargs['libs'] = ldlibs

        if rule.libs:
            variables[ldlibs] = [global_ldlibs] + list(chain.from_iterable(
                linker.link_lib(i) for i in rule.libs
            ))

    if ldflags_value:
        variables[ldflags] = [global_ldflags] + ldflags_value

    if not writer.has_variable(recipename):
        writer.variable(recipename, [
            linker.command(
                cmd=cmd_var(linker, writer), input=var('1'), output=var('@'),
                args=ldflags, **command_kwargs
            )
        ], flavor='define')

    writer.rule(
        target=path,
        deps=[i.path for i in chain(rule.files, lib_deps, rule.extra_deps)],
        order_only=[target_dir.append(dir_sentinel)] if target_dir else None,
        recipe=MakeCall(recipename, (i.path for i in rule.files)),
        variables=variables
    )

@rule_handler('Alias')
def emit_alias(rule, build_inputs, writer):
    writer.rule(
        target=rule.target.path,
        deps=[i.path for i in rule.extra_deps],
        phony=True
    )

@rule_handler('Command')
def emit_command(rule, build_inputs, writer):
    writer.rule(
        target=rule.target.path,
        deps=[i.path for i in rule.extra_deps],
        recipe=rule.cmds,
        phony=True
    )
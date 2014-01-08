from random import Random
from contextlib import contextmanager
from .operations import (
    ChooseFrom,
    ReadAndWrite,
    Check,
    PushRandom,
    BinaryOperator,
    UnaryOperator,
    Dup,
    Drop,
)
from collections import namedtuple, Counter
import operator
import traceback
import math


ProgramStep = namedtuple(
    "ProgramStep",
    ("definitions", "arguments", "operation")
)


class TestMachineError(Exception):
    pass


class FrozenVarStack(TestMachineError):
    def __init__(self):
        super(FrozenVarStack, self).__init__("Cannot modify frozen varstack")


class NoFailingProgram(TestMachineError):
    pass


class VarStack(object):
    def __init__(self, name, context):
        self.name = name
        self.context = context
        self.data = []
        self.names = []
        self.frozen = False

    def _integrity_check(self):
        assert len(self.data) == len(self.names)

    @contextmanager
    def freeze(self):
        self.frozen = True
        try:
            yield
        finally:
            self.frozen = False

    def modification(self):
        if self.frozen:
            raise FrozenVarStack()

    def pop(self):
        self.modification()
        self._integrity_check()
        result = self.data.pop()
        self.context.on_read(self.names.pop())
        return result

    def push(self, head):
        self.modification()
        self._integrity_check()
        self.data.append(head)
        v = self.context.newvar()
        self.names.append(v)
        self.context.on_write(v)

    def dup(self):
        self.modification()
        self._integrity_check()
        self.names.append(self.names[-1])
        self.data.append(self.data[-1])

    def peek(self, index=0):
        self._integrity_check()
        i = -1 - index
        self.context.on_read(self.names[i])
        return self.data[i]

    def has(self, count):
        self._integrity_check()
        return len(self.data) >= count


class RunContext(object):
    def __init__(self, random=None):
        self.random = random or Random()
        self.varstacks = {}
        self.var_index = 0
        self.reset_tracking()

    def reset_tracking(self):
        self.values_read = []
        self.values_written = []

    def __repr__(self):
        return "RunContext(%s)" % (
            ', '.join(
                "%s=%r" % (v.name, len(v.data))
                for v in self.varstacks.values()
            )
        )

    def newvar(self):
        self.var_index += 1
        return "t%d" % (self.var_index,)

    def on_read(self, var):
        self.values_read.append(var)

    def on_write(self, var):
        self.values_written.append(var)

    def varstack(self, name):
        try:
            return self.varstacks[name]
        except KeyError:
            varstack = VarStack(name, self)
            self.varstacks[name] = varstack
            return varstack

    def read(self, argspec):
        result = []
        seen = Counter()
        for a in argspec:
            result.append(self.varstack(a).peek(seen[a]))
            seen[a] += 1
        return tuple(result)


class TestMachine(object):
    def __init__(
        self,
        n_iters=500,
        prog_length=200,
        good_enough=10
    ):
        self.languages = []
        self.n_iters = n_iters
        self.prog_length = prog_length
        self.good_enough = good_enough

    def operation(self, *args, **kwargs):
        """
        Add an operation which pops arguments from each of the varstacks named
        in args, passes the result in that order to function and pushes the
        result of the invocation onto target. If target is None the result is
        ignored.
        """
        self.add_language(ReadAndWrite(
            *args, **kwargs
        ))

    def check(self, *args, **kwargs):
        """
        Add an operation which reads from the varstacks in args in order,
        without popping their result and passes them in order to test. If test
        returns something truthy this operation passes, else it will fail.
        """
        self.add_language(Check(*args, **kwargs))

    def generate(self, produce, target, name=None):
        """
        Add a generator for operations which produces values by calling
        produce with a Random instance and pushes them onto target.
        """
        self.add_language(
            PushRandom(produce=produce, target=target, name=name)
        )

    def binary_operation(self, *args, **kwargs):
        self.add_language(
            BinaryOperator(*args, **kwargs)
        )

    def unary_operation(self, operation, varstack, name):
        self.add_language(
            UnaryOperator(operation, varstack, name)
        )

    def basic_operations(self, varstack):
        self.add_language(Dup(varstack))
        self.add_language(Drop(varstack))

    def ints(self, target):
        self.basic_operations(target)
        self.arithmetic_operations(target)
        self.generate(lambda r: r.randint(0, 10 ** 6), target)
        self.generate(lambda r: r.randint(-10, 10), target)

    def lists(self, source, target):
        self.basic_operations(target)
        self.generate(lambda r: [], target)
        self.operation(
            function=lambda x, y: x.append(y),
            argspec=(target, source),
            target=None,
            name="append",
            pattern="%s.append(%s)"
        )
        self.operation(
            function=lambda x: [x],
            argspec=(source,),
            target=target,
            name="singleton",
            pattern="[%s]",
        )
        self.operation(
            function=lambda x, y: [x, y],
            argspec=(source, source),
            target=target,
            name="pair",
            pattern="[%s, %s]",
        )
        self.operation(
            function=list,
            argspec=(target,),
            target=target
        )
        self.binary_operation(operator.add, target, "+")

    def arithmetic_operations(self, varstack):
        self.binary_operation(operator.add, varstack, "+")
        self.binary_operation(operator.sub, varstack, "-")
        self.binary_operation(operator.mul, varstack, "*")
        self.binary_operation(
            operator.div, varstack, "/",
            precondition=lambda x, y: y != 0
        )
        self.unary_operation(operator.neg, varstack, "-")

    def power_operation(self, varstack):
        self.binary_operation(
            operator.pow, varstack, "**",
            precondition=lambda x, y: (x > 0) or (math.floor(y) == y)
        )

    def add_language(self, language):
        self.languages.append(language)

    @property
    def language(self):
        return ChooseFrom(self.languages)

    def find_failing_program(
        self,
    ):
        examples_found = 0
        best_example = None

        for _ in xrange(self.n_iters):
            program = []
            context = RunContext()
            for _ in xrange(self.prog_length):
                operation = self.language.generate(context)
                program.append(operation)
                try:
                    operation.invoke(context)
                except Exception:
                    examples_found += 1
                    if (
                        (best_example is None) or
                        (len(program) < len(best_example))
                    ):
                        best_example = program
                    if examples_found >= self.good_enough:
                        return best_example
                    else:
                        break
        if best_example is None:
            raise NoFailingProgram(
                ("Unable to find a failing program of length <= %d"
                 " after %d iterations") % (self.prog_length, self.n_iters)
            )
        return best_example

    def run_program(self, program):
        context = RunContext()
        for operation in program:
            operation.invoke(context)
        return context

    def program_fails(self, program):
        try:
            self.run_program(program)
            return False
        except Exception:
            return True

    def prune_program(self, program):
        context = RunContext()
        results = []
        for operation in program:
            if not operation.applicable(context):
                continue
            results.append(operation)
            try:
                operation.invoke(context)
            except Exception:
                break

        return results

    def minimize_failing_program(self, program):
        assert self.program_fails(program)
        current_best = program
        while True:
            for i in xrange(len(current_best)):
                edit = list(current_best)
                del edit[i]
                pruned_edit = self.prune_program(edit)
                if self.program_fails(pruned_edit):
                    current_best = pruned_edit
                    break
                if i < len(edit):
                    del edit[i]
                    pruned_edit = self.prune_program(edit)
                    if self.program_fails(pruned_edit):
                        current_best = pruned_edit
                        break
            else:
                return current_best

    def annotate_program(self, program):
        context = RunContext()
        results = []
        for op in program:
            had_error = False
            context.reset_tracking()
            try:
                op.invoke(context)
            except Exception:
                had_error = True

            results.append(ProgramStep(
                operation=op,
                definitions=(
                    () if had_error else tuple(context.values_written)
                ),
                arguments=tuple(context.values_read)
            ))

            if had_error:
                break

        return results

    def run(self):
        try:
            first_try = self.find_failing_program()
        except NoFailingProgram as e:
            print e.message
            return
        minimal = self.minimize_failing_program(first_try)
        annotated = self.annotate_program(minimal)
        for step in annotated:
            statements = step.operation.compile(
                arguments=step.arguments, results=step.definitions
            )
            for statement in statements:
                print statement
        try:
            self.run_program(minimal)
            assert False, "This program should be failing but isn't"
        except Exception:
            traceback.print_exc()

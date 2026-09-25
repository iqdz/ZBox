"""
A permissive stand-in for wxPython.

Nothing here runs a GUI. The point is narrower and worth having: it
lets every ZBox module be imported on a machine with no wxPython, so
the module-level code -- class bases, id allocation, constants, and
above all the import graph -- is checked on every test run.

That matters while main_frame.py is being split apart. Moving a
class between modules is exactly the kind of change that leaves a
name behind or quietly introduces an import cycle, and both fail at
import time, which is the one failure a screen reader user
experiences as "ZBox will not start".

Any attribute of anything is Stub, and Stub accepts any call,
attribute or bitwise combination, so wx.Panel works as a base class
and wx.ACCEL_CTRL | wx.ACCEL_SHIFT does not raise.
"""


class _Meta(type):
    def __getattr__(cls, name):
        return Stub

    def __or__(cls, other):
        return Stub

    def __ror__(cls, other):
        return Stub


class Stub(metaclass=_Meta):
    def __init__(self, *args, **kwargs):
        pass

    def __getattr__(self, name):
        return Stub()

    def __call__(self, *args, **kwargs):
        return Stub()

    def __or__(self, other):
        return Stub()

    def __ror__(self, other):
        return Stub()

    def __and__(self, other):
        return Stub()

    def __int__(self):
        return 0

    def __index__(self):
        return 0


def __getattr__(name):
    return Stub

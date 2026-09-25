ZBox tests.

Run from the project root:

    python -m unittest discover -s tests -v

No wxPython needed. Everything covered here is deliberately pure --
given a dict, return a string or a bool -- which is why these
modules were the first thing split out of main_frame.py. The
fixtures are the real shapes Himalaya 2.1.0 returns, captured live,
not invented ones; several of the bugs these guard against came from
assuming the wrong shape.

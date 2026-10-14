"""A seeded lifecycle simulation that checks liveness after every step.

The world (``world``) is the real Controller services over one PostgreSQL
database with a fake clock; ``sparks`` are fake Spark agents that answer the
orders those services issue; ``actions`` are what operators, Sparks and faults
do to it; ``invariants`` are the liveness properties checked after every step;
``simulation`` draws a seeded sequence of actions and reports a failing seed
with its step trace.
"""

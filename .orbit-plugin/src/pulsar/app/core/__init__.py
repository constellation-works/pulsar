"""The domain beneath the app: ``account`` (who pulsar posts as),
``ledger`` (every write, committed before it leaves), ``publishing`` (a plan
into posts) and ``channels`` (where posts go). Only ``app`` uses these;
nothing here imports ``app``.

Inside, ``publishing`` stands on ``ledger``, ``account`` and ``channels``;
``account`` on ``channels``; ``channels`` and ``ledger`` on nothing else here.
"""

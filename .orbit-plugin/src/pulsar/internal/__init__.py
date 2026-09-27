"""Leaf utilities anything in pulsar may use, and which use nothing of pulsar
above them: the error vocabulary (``errors``), owner-only files, the home
layout and JSON views (``fs``), and the secret scanner (``guard``).

This ``__init__`` stays empty so importing one of them loads only that one.
"""

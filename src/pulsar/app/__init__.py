"""The application: what every front end calls.

The modules here consume ``core`` and join it into the verbs: the ``App`` and
``Runtime`` protocols (``interfaces``), ``LocalApp`` (``facade``) over
``LocalRuntime`` (``runtime``), the operator verbs (``ops``), account health
(``health``), ``config.toml`` (``settings``), the ``writes.jsonl`` export
(``writelog``) and the ``posted.jsonl`` import (``importer``). Front ends
import these modules, and reach ``core`` only through ``exports``.

This ``__init__`` stays empty so importing a module here, or anything under
``core``, does not load the rest of the app.
"""

"""Analytical core of the subsystem.

Pure algorithms (numpy only): no web framework, database or Redis imports, so the
core can be tested and run offline on historical data independently of the API.

Time is represented everywhere as int64 microseconds since the Unix epoch
(the native resolution of QuestDB timestamps).
"""

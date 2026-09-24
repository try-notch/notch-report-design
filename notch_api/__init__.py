"""
notch_api — a local server that speaks the notch-ios-dev contract.

The demo CLI next door (db.py, tagger.py, generate_report.py, ...) proves the
prompts. This package puts them behind the API the iOS app expects: the §3
schema in SQLite, the §5 wire shapes, audio in and a report out, every model
call through OpenRouter. SERVER.md says how to run it and where it deviates.
"""

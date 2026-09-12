"""Broker integrations.

Currently one broker — Angel One via its SmartAPI REST contract — but the
package keeps the door open: every broker lives under `broker/` with a
`<name>_client` (thin HTTP) and a `<name>_service` (product logic), and
shares `exceptions.py` for the error vocabulary.
"""

"""Query and aggregation helpers shared by several routers.

Routers stay thin: they parse and validate, call something here, and serialize.  Anything
that is a *question about the data* — "the page of calls matching this filter", "the
facets", "the correlation candidates around this instant" — lives in this package so the
WebSocket feed, the exports and the HTTP routes all answer it the same way.
"""

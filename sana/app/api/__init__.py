"""FastAPI transport for Sana.

Import ``sana.app.api.main`` explicitly to construct an application. Keeping the
package initializer side-effect free prevents database and Redis clients from
being allocated when a schema or dependency type is imported.
"""

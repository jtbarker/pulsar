'''Asynchronous WSGI_ Remote Procedure Calls middleware. It implements a
JSON-RPC_ server and client. Check out the
:ref:`json-rpc tutorial <tutorials-calculator>` if you want to get started
quickly with a working example.

API
===========

RpcHandler
~~~~~~~~~~~~~~

.. autoclass:: RpcHandler
   :members:
   :member-order: bysource


JSON RPC
~~~~~~~~~~~~~~~~

.. autoclass:: JSONRPC
   :members:
   :member-order: bysource


JsonProxy
~~~~~~~~~~~~~~~~

.. autoclass:: JsonProxy
   :members:
   :member-order: bysource


rpc method decorator
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. autofunction:: rpc_method


Server Commands
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. autoclass:: PulsarServerCommands
   :members:
   :member-order: bysource

.. _JSON-RPC: http://www.jsonrpc.org/specification
.. _WSGI: http://www.python.org/dev/peps/pep-3333/
'''
from .handlers import *
from .jsonrpc import *
from .mixins import *

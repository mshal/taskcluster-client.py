"""This module is used to interact with taskcluster rest apis"""

import os
import json
import logging
import copy
try:
  import urlparse
except ImportError:
  import urllib.parse as urlparse
import time
import hashlib
import hmac
import datetime
import calendar

# For finding apis.json
from pkg_resources import resource_string
import requests
import hawk

from . import exceptions
from . import utils

log = logging.getLogger(__name__)
log.setLevel(logging.DEBUG)
# Only for debugging XXX: turn off the True or thing when shipping
if os.environ.get('DEBUG_TASKCLUSTER_CLIENT'):
  log.addHandler(logging.StreamHandler())
log.addHandler(logging.NullHandler())

API_CONFIG = json.loads(resource_string(__name__, 'apis.json').decode('utf-8'))


# Default configuration
_defaultConfig = config = {
  'credentials': {
    'clientId': os.environ.get('TASKCLUSTER_CLIENT_ID'),
    'accessToken': os.environ.get('TASKCLUSTER_ACCESS_TOKEN'),
    'certificate': os.environ.get('TASKCLUSTER_CERTIFICATE'),
  },
  'maxRetries': 5,
  'signedUrlExpiration': 15 * 60,
}


class BaseClient(object):
  """ Base Class for API Client Classes. Each individual Client class
  needs to set up its own methods for REST endpoints and Topic Exchange
  routing key patterns.  The _makeApiCall() and _topicExchange() methods
  help with this.
  """

  def __init__(self, options=None):
    o = self.options = copy.deepcopy(self.options)
    o.update(_defaultConfig)
    if options:
      o.update(options)
    log.debug(o)

  def makeHawkExt(self):
    """ Make an 'ext' for Hawk authentication """
    o = self.options
    c = o.get('credentials', {})
    if c.get('clientId') and c.get('accessToken'):
      ext = {}
      cert = c.get('certificate')
      if cert:
        if isinstance(cert, basestring):
          cert = json.loads(cert)
        ext['certificate'] = cert

      if 'authorizedScopes' in o:
        ext['authorizedScopes'] = o['authorizedScopes']

      # .encode('base64') inserts a newline, which hawk doesn't
      # like but doesn't strip itself
      return utils.makeB64UrlSafe(utils.encodeStringForB64Header(utils.dumpJson(ext)).strip())
    else:
      return {}

  def _makeTopicExchange(self, entry, *args, **kwargs):
    if len(args) == 0 and not kwargs:
      routingKeyPattern = {}
    elif len(args) >= 1:
      if kwargs or len(args) != 1:
        errStr = 'Pass either a string, single dictionary or only kwargs'
        raise exceptions.TaskclusterTopicExchangeFailure(errStr)
      routingKeyPattern = args[0]
    else:
      routingKeyPattern = kwargs

    data = {
      'exchange': '%s/%s' % (self.options['exchangePrefix'].rstrip('/'),
                             entry['exchange'].lstrip('/'))
    }

    # If we are passed in a string, we can short-circuit this function
    if isinstance(routingKeyPattern, basestring):
      log.debug('Passing through string for topic exchange key')
      data['routingKeyPattern'] = routingKeyPattern
      return data

    if type(routingKeyPattern) != dict:
      errStr = 'routingKeyPattern must eventually be a dict'
      raise exceptions.TaskclusterTopicExchangeFailure(errStr)

    if not routingKeyPattern:
      routingKeyPattern = {}

    # There is no canonical meaning for the maxSize and required
    # reference entry in the JS client, so we don't try to define
    # them here, even though they sound pretty obvious

    routingKey = []
    for key in entry['routingKey']:
      if 'constant' in key:
        value = key['constant']
      elif key['name'] in routingKeyPattern:
        log.debug('Found %s in routing key params', key['name'])
        value = str(routingKeyPattern[key['name']])
        if not key.get('multipleWords') and '.' in value:
          raise exceptions.TaskclusterTopicExchangeFailure('Cannot have periods in single word keys')
      else:
        value = '#' if key.get('multipleWords') else '*'
        log.debug('Did not find %s in input params, using %s', key['name'], value)

      routingKey.append(value)

    data['routingKeyPattern'] = '.'.join([str(x) for x in routingKey])
    return data

  def buildUrl(self, methodName, *args, **kwargs):
    entry = None
    for x in self._api['entries']:
      if x['name'] == methodName:
        entry = x
    if not entry:
      raise exceptions.TaskclusterFailure('Requested method "%s" not found in API Reference' % methodName)
    apiArgs = self._processArgs(entry, *args, **kwargs)
    route = self._subArgsInRoute(entry, apiArgs)
    return self.options['baseUrl'] + '/' + route

  def buildSignedUrl(self, methodName, *args, **kwargs):
    """ Build a signed URL.  This URL contains the credentials needed to access
    a resource."""

    if 'expiration' in kwargs:
      expiration = kwargs['expiration']
      del kwargs['expiration']
    else:
      expiration = self.options['signedUrlExpiration']

    expiration = float(expiration)  # Mainly so that we throw if it's not a number

    requestUrl = self.buildUrl(methodName, *args, **kwargs)

    opts = self.options
    cred = opts.get('credentials')
    if not cred or 'clientId' not in cred or 'accessToken' not in cred:
      raise exceptions.TaskclusterAuthFailure('Invalid Hawk Credentials')

    clientId = self.options['credentials']['clientId']
    accessToken = self.options['credentials']['accessToken']

    bewitOpts = {
      'credentials': {
        'id': clientId,
        'key': accessToken,
        'algorithm': 'sha256',
      },
      'ttl_sec': expiration,
      'ext': self.makeHawkExt(),
    }

    def genBewit():
      # We need to fix the output of get_bewit.  It returns a base64 encoded
      # string, which contains a list of tokens seperated by '\' characters.
      # The first one is the clientId, the second is an int, the third is some
      # base64 encoded data, the fourth is the ext param.  The problem is that
      # the nested base64 must be normal (i.e. not url safe) base64 for the
      # server to understand.
      _bewit = hawk.client.get_bewit(requestUrl, bewitOpts)
      decoded = _bewit.decode('base64')
      decodedParts = decoded.split('\\')
      decodedParts[2] = utils.makeB64UrlUnsafe(decodedParts[2])
      return utils.makeB64UrlSafe(utils.encodeStringForB64Header('\\'.join(decodedParts)))

    bewit = genBewit()
    bewitDecoded = bewit.decode('base64')

    if not bewit:
      raise exceptions.TaskclusterFailure('Did not receive a bewit')

    if '_' in bewitDecoded or '-' in bewitDecoded:
      raise exceptions.TaskclusterFailure('Not sure why this causes server side failure!')

    u = urlparse.urlparse(requestUrl)
    return urlparse.urlunparse((
      u.scheme,
      u.netloc,
      u.path,
      u.params,
      u.query + 'bewit=%s' % bewit,
      u.fragment,
    ))

  def _makeApiCall(self, entry, *args, **kwargs):
    """ This function is used to dispatch calls to other functions
    for a given API Reference entry"""

    payload = None
    _args = list(args)
    _kwargs = copy.deepcopy(kwargs)

    if 'input' in entry:
      if len(args) > 0:
        payload = _args.pop()
      else:
        raise exceptions.TaskclusterFailure('Payload is required as last positional arg')

    apiArgs = self._processArgs(entry, *_args, **_kwargs)
    route = self._subArgsInRoute(entry, apiArgs)
    log.debug('Route is: %s', route)

    return self._makeHttpRequest(entry['method'], route, payload)

  def _processArgs(self, entry, *args, **kwargs):
    """ Take the list of required arguments, positional arguments
    and keyword arguments and return a dictionary which maps the
    value of the given arguments to the required parameters.

    Keyword arguments will overwrite positional arguments.
    """

    reqArgs = entry['args']
    data = {}

    # These all need to be rendered down to a string, let's just check that
    # they are up front and fail fast
    for arg in list(args) + [kwargs[x] for x in kwargs]:
      if not isinstance(arg, basestring):
        raise exceptions.TaskclusterFailure('Arguments "%s" to %s is not a string' % (arg, entry['name']))

    if len(args) > 0 and len(kwargs) > 0:
      raise exceptions.TaskclusterFailure('Specify either positional or key word arguments')

    # We know for sure that if we don't give enough arguments that the call
    # should fail.  We don't yet know if we should fail because of two many
    # arguments because we might be overwriting positional ones with kw ones
    if len(reqArgs) > len(args) + len(kwargs):
      raise exceptions.TaskclusterFailure('%s takes %d args, only %d were given' % (
                                          entry['name'], len(reqArgs), len(args) + len(kwargs)))

    # We also need to error out when we have more positional args than required
    # because we'll need to go through the lists of provided and required args
    # at the same time.  Not disqualifying early means we'll get IndexErrors if
    # there are more positional arguments than required
    if len(args) > len(reqArgs):
      raise exceptions.TaskclusterFailure('%s called with too many positional args', entry['name'])

    i = 0
    for arg in args:
      log.debug('Found a positional argument: %s', arg)
      data[reqArgs[i]] = arg
      i += 1

    log.debug('After processing positional arguments, we have: %s', data)

    data.update(kwargs)

    log.debug('After keyword arguments, we have: %s', data)

    if len(reqArgs) != len(data):
      errMsg = '%s takes %s args, %s given' % (
        entry['name'],
        ','.join(reqArgs),
        data.keys())
      log.error(errMsg)
      raise exceptions.TaskclusterFailure(errMsg)

    for reqArg in reqArgs:
      if reqArg not in data:
        errMsg = '%s requires a "%s" argument which was not provided' % (entry['name'], reqArg)
        log.error(errMsg)
        raise exceptions.TaskclusterFailure(errMsg)

    return data

  def _subArgsInRoute(self, entry, args):
    """ Given a route like "/task/<taskId>/artifacts" and a mapping like
    {"taskId": "12345"}, return a string like "/task/12345/artifacts"
    """

    route = entry['route']

    for arg, val in args.iteritems():
      toReplace = "<%s>" % arg
      if toReplace not in route:
        raise exceptions.TaskclusterFailure('Arg %s not found in route for %s' % (arg, entry['name']))
      route = route.replace("<%s>" % arg, val)

    return route.lstrip('/')

  def _makeHttpRequest(self, method, route, payload):
    """ Make an HTTP Request for the API endpoint.  This method wraps
    the logic about doing failure retry and passes off the actual work
    of doing an HTTP request to another method."""

    baseUrl = self.options['baseUrl']
    # urljoin ignores the last param of the baseUrl if the base url doesn't end
    # in /.  I wonder if it's better to just do something basic like baseUrl +
    # route instead
    if not baseUrl.endswith('/'):
      baseUrl += '/'
    fullUrl = urlparse.urljoin(baseUrl, route.lstrip('/'))
    log.debug('Full URL used is: %s', fullUrl)

    retry = 0
    response = None
    error = None
    while retry < self.options['maxRetries']:
      retry += 1
      log.debug('Making attempt %d', retry)
      try:
        response = self._makeSingleHttpRequest(method, fullUrl, payload, self.makeHawkExt())
      except requests.exceptions.RequestException as rerr:
        error = True
        if retry >= self.options['maxRetries']:
          raise exceptions.TaskclusterRestFailure('Last Attempt: %s' % rerr, superExc=rerr)
        else:
          log.warn('Retrying because of: %s' % rerr)

      # We only want to consider connection errors for retry.  Other errors,
      # like a 404 saying that a resource wasn't found should be returned
      # immediately
      if response and response.status_code >= 500 and response.status_code < 600:
        log.warn('Received HTTP Status %d, retrying', response.status_code)
        error = True

      if error:
        snooze = float(retry * retry) / 10.0
        log.info('Sleeping %0.2f seconds for exponential backoff', snooze)
        time.sleep(snooze)
      else:
        break

    # We want to send JSON data back to the caller
    try:
      # We want to make sure that calling code handles errors
      response.raise_for_status()
      apiData = response.json()
    except requests.exceptions.RequestException as rerr:
      if response.status_code == 401:
        raise exceptions.TaskclusterAuthFailure('Server rejected our credentials')
      else:
        raise exceptions.TaskclusterRestFailure('Request failed', superExc=rerr)
    except ValueError as ve:
      errStr = 'Response contained malformed JSON data'
      log.error(errStr)
      raise exceptions.TaskclusterRestFailure(errStr, superExc=ve, res=response)

    return apiData

  def _makeSingleHttpRequest(self, method, url, payload, hawkExt=None):
    log.debug('Making a %s request to %s', method, url)
    opts = self.options
    cred = opts.get('credentials')
    if cred and 'clientId' in cred and 'accessToken' in cred and cred['clientId'] and cred['accessToken']:
      hawkOpts = {
        'credentials': {
          'id': cred['clientId'],
          'key': cred['accessToken'],
          'algorithm': 'sha256',
        },
        'ext': hawkExt if hawkExt else {},
      }
      header = hawk.client.header(url, method, hawkOpts)
      headers = {'Authorization': header['field'].strip()}
    else:
      log.info('Not using hawk!')
      headers = {}
    if payload:
      payload = utils.dumpJson(payload)
      headers['Content-Type'] = 'application/json'

    return requests.request(method.upper(), url, data=payload, headers=headers)


def createApiClient(name, api):
  attributes = dict(
    name=name,
    _api=api['reference'],
    __doc__=api.get('description'),
    options={},
  )

  copiedOptions = ('baseUrl', 'exchangePrefix')
  for opt in copiedOptions:
    if opt in api['reference']:
      attributes['options'][opt] = api['reference'][opt]

  for entry in api['reference']['entries']:
    if entry['type'] == 'function':

      def addApiCall(e):
        def apiCall(self, *args, **kwargs):
          return self._makeApiCall(e, *args, **kwargs)
        return apiCall

      f = addApiCall(entry)
      docStr = "Call the %s api's %s method.  " % (name, entry['name'])

      if entry['args'] and len(entry['args']) > 0:
        docStr += "This method takes:\n\n"
        docStr += '\n'.join(['- ``%s``' % x for x in entry['args']])
        docStr += '\n\n'
      else:
        docStr += "This method takes no arguments.  "

      if 'input' in entry:
        docStr += "This method takes input ``%s``.  " % entry['input']

      if 'output' in entry:
        docStr += "This method gives output ``%s``" % entry['output']

      docStr += '\n\nThis method does a ``%s`` to ``%s``.' % (entry['method'].upper(), entry['route'])

      f.__doc__ = docStr

    elif entry['type'] == 'topic-exchange':
      def addTopicExchange(e):
        def topicExchange(self, *args, **kwargs):
          return self._makeTopicExchange(e, *args, **kwargs)
        return topicExchange

      f = addTopicExchange(entry)

      docStr = 'Generate a routing key pattern for the %s exchange.  ' % entry['exchange']
      docStr += 'This method takes a given routing key as a string or a '
      docStr += 'dictionary.  For each given dictionary key, the corresponding '
      docStr += 'routing key token takes its value.  For routing key tokens '
      docStr += 'which are not specified by the dictionary, the * or # character '
      docStr += 'is used depending on whether or not the key allows multiple words.\n\n'
      docStr += 'This exchange takes the following keys:\n\n'
      docStr += '\n'.join(['- ``%s``' % x['name'] for x in entry['routingKey']])

      f.__doc__ = docStr

    # Give the function the right name
    f.__name__ = str(entry['name'])

    # Add whichever function we created
    attributes[entry['name']] = f

  return type(name.encode('utf-8'), (BaseClient,), attributes)


def createTemporaryCredentials(clientId, accessToken, start, expiry, scopes):
  """ Create a set of temporary credentials

  start: start time of credentials, seconds since epoch
  expiry: expiration time of credentials, seconds since epoch
  scopes: list of scopes granted
  credentials: { 'clientId': None, 'accessToken': None }
               credentials to use to generate temporary credentials

  Returns a dictionary in the form:
    { 'clientId': str, 'accessToken: str, 'certificate': str}
  """

  now = datetime.datetime.utcnow()
  now = now - datetime.timedelta(minutes=10)  # Subtract 5 minutes for clock drift

  for scope in scopes:
    if not isinstance(scope, basestring):
      raise exceptions.TaskclusterFailure('Scope must be string')

  # Credentials can only be valid for 31 days.  I hope that
  # this is validated on the server somehow...

  if expiry - start > datetime.timedelta(days=31):
    raise exceptions.TaskclusterFailure('Only 31 days allowed')

  cert = dict(
    version=1,
    scopes=scopes,
    start=calendar.timegm(start.utctimetuple()),
    expiry=calendar.timegm(expiry.utctimetuple()),
    seed=utils.slugId() + utils.slugId(),
  )

  sigStr = '\n'.join([
    'version:' + str(cert['version']),
    'seed:' + cert['seed'],
    'start:' + str(cert['start']),
    'expiry:' + str(cert['expiry']),
    'scopes:'
  ] + scopes)

  sig = hmac.new(accessToken, sigStr, hashlib.sha256).digest()

  cert['signature'] = utils.encodeStringForB64Header(sig)

  newToken = hmac.new(accessToken, cert['seed'], hashlib.sha256).digest()
  newToken = utils.makeB64UrlSafe(utils.encodeStringForB64Header(newToken)).replace('=', '')

  return {
    'clientId': clientId,
    'accessToken': newToken,
    'certificate': utils.dumpJson(cert),
  }

__all__ = ['createTemporaryCredentials', 'config']
# This has to be done after the Client class is declared
for key, value in API_CONFIG.items():
  globals()[key] = createApiClient(key, value)
  __all__.append(key)

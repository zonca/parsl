import logging
import json
import globus_sdk
import os


logger = logging.getLogger(__name__)
# Add StreamHandler to print error Globus events to stderr
handler = logging.StreamHandler()
handler.setLevel(logging.WARN)
format_string = "%(asctime)s %(name)s:%(lineno)d [%(levelname)s]  %(message)s"
formatter = logging.Formatter(format_string, datefmt='%Y-%m-%d %H:%M:%S')
handler.setFormatter(formatter)
logger.addHandler(handler)


"""
'Parsl Application' OAuth2 client registered with Globus Auth
by lukasz@globusid.org
"""
CLIENT_ID = '8b8060fd-610e-4a74-885e-1051c71ad473'
REDIRECT_URI = 'https://auth.globus.org/v2/web/auth-code'
SCOPES = ('openid '
          'urn:globus:auth:scope:transfer.api.globus.org:all')

token_path = os.path.join(os.path.expanduser('~'), '.parsl')
if not os.path.isdir(token_path):
    os.mkdir(token_path)
TOKEN_FILE = os.path.join(token_path, '.globus.json')

get_input = getattr(__builtins__, 'raw_input', input)


def _load_tokens_from_file(filepath):
    with open(filepath, 'r') as f:
        tokens = json.load(f)
    return tokens


def _save_tokens_to_file(filepath, tokens):
    with open(filepath, 'w') as f:
        json.dump(tokens, f)


def _update_tokens_file_on_refresh(token_response):
    _save_tokens_to_file(TOKEN_FILE, token_response.by_resource_server)


def _do_native_app_authentication(client_id, redirect_uri,
                                  requested_scopes=None):

    client = globus_sdk.NativeAppAuthClient(client_id=client_id)
    client.oauth2_start_flow(
        requested_scopes=requested_scopes,
        redirect_uri=redirect_uri,
        refresh_tokens=True)

    url = client.oauth2_get_authorize_url()
    print('Please visit the following URL to provide authorization: \n{}'.format(url))
    auth_code = get_input('Enter the auth code: ').strip()
    token_response = client.oauth2_exchange_code_for_tokens(auth_code)
    return token_response.by_resource_server


def _get_native_app_authorizer(client_id):
    tokens = None
    try:
        tokens = _load_tokens_from_file(TOKEN_FILE)
    except Exception:
        pass

    if not tokens:
        tokens = _do_native_app_authentication(
            client_id=client_id,
            redirect_uri=REDIRECT_URI,
            requested_scopes=SCOPES)
        try:
            _save_tokens_to_file(TOKEN_FILE, tokens)
        except Exception:
            pass

    transfer_tokens = tokens['transfer.api.globus.org']

    auth_client = globus_sdk.NativeAppAuthClient(client_id=client_id)

    return globus_sdk.RefreshTokenAuthorizer(
        transfer_tokens['refresh_token'],
        auth_client,
        access_token=transfer_tokens['access_token'],
        expires_at=transfer_tokens['expires_at_seconds'],
        on_refresh=_update_tokens_file_on_refresh)


def get_globus():
    Globus.init()
    return Globus()


class Globus(object):
    """
    All communication with the Globus Auth and Globus Transfer services is enclosed
    in the Globus class. In particular, the Globus class is reponsible for:
     - managing an OAuth2 authorizer - getting access and refresh tokens,
       refreshing an access token, storing to and retrieving tokens from
       .globus.json file,
     - submitting file transfers,
     - monitoring transfers.
    """

    authorizer = None

    @classmethod
    def init(cls):
        if cls.authorizer:
            return
        cls.authorizer = _get_native_app_authorizer(CLIENT_ID)

    @classmethod
    def get_authorizer(cls):
        return cls.authorizer

    @classmethod
    def transfer_file(cls, src_ep, dst_ep, src_path, dst_path):
        tc = globus_sdk.TransferClient(authorizer=cls.authorizer)
        td = globus_sdk.TransferData(tc, src_ep, dst_ep)
        td.add_item(src_path, dst_path)
        try:
            task = tc.submit_transfer(td)
        except Exception as e:
            raise Exception('Globus transfer from {}{} to {}{} failed due to error: {}'.format(
                src_ep, src_path, dst_ep, dst_path, e))

        last_event_time = None
        """
        A Globus transfer job (task) can be in one of the three states: ACTIVE, SUCCEEDED, FAILED.
        Parsl every 20 seconds polls a status of the transfer job (task) from the Globus Transfer service,
        with 60 second timeout limit. If the task is ACTIVE after time runs out 'task_wait' returns False,
        and True otherwise.
        """
        while not tc.task_wait(task['task_id'], 60, 15):
            task = tc.get_task(task['task_id'])
            # Get the last error Globus event
            events = tc.task_event_list(task['task_id'], num_results=1, filter='is_error:1')
            event = events.data[0]
            # Print the error event to stderr and Parsl file log if it was not yet printed
            if event['time'] != last_event_time:
                last_event_time = event['time']
                logger.warn('Non-critical Globus Transfer error event for globus://{}{}: "{}" at {}. Retrying...'.format(
                    src_ep, src_path, event['description'], event['time']))
                logger.debug('Globus Transfer error details: {}'.format(event['details']))

        """
        The Globus transfer job (task) has been terminated (is not ACTIVE). Check if the transfer
        SUCCEEDED or FAILED.
        """
        task = tc.get_task(task['task_id'])
        if task['status'] == 'SUCCEEDED':
            logger.debug('Globus transfer {}, from {}{} to {}{} succeeded'.format(
                task['task_id'], src_ep, src_path, dst_ep, dst_path))
        else:
            logger.debug('Globus Transfer task: {}'.format(task))
            events = tc.task_event_list(task['task_id'], num_results=1, filter='is_error:1')
            event = events.data[0]
            raise Exception('Globus transfer {}, from {}{} to {}{} failed due to error: "{}"'.format(
                task['task_id'], src_ep, src_path, dst_ep, dst_path, event['details']))

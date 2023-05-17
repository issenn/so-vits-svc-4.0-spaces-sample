import os
import time
import re
import shutil
from pathlib import Path
import tempfile
import urllib.parse
import requests
import json
import logging
from configparser import BasicInterpolation, ConfigParser

import gradio as gr

import msal
from msal import ClientApplication
from office365.graph_client import GraphClient
from office365.onedrive.drives.drive import Drive
from office365.onedrive.driveitems.driveItem import DriveItem

import numpy as np
import torchaudio
import torch
from so_vits_svc_fork.hparams import HParams
from so_vits_svc_fork.inference.core import Svc
import librosa

import modules.ui


store = {
    "accounts": [],
    "models": {},
    "response_mode": "query",
    "all_scopes": [
        "https://graph.microsoft.com/.default",
        "User.Read",
        "Files.Read",
        "Files.Read.All"
    ],
}

class SecEnvInterpolation(BasicInterpolation):
    secure_vars = os.environ.get('office365_python_sdk_securevars', '').split(';')
    env = {
        "tenant": os.environ.get('ONEDRIVE_TENANT'),
        "username": os.environ.get('ONEDRIVE_USERNAME'),
        "password": os.environ.get('ONEDRIVE_PASSWORD'),
        "client_id": os.environ.get('ONEDRIVE_CLIENT_ID'),
        "client_secret": os.environ.get('ONEDRIVE_CLIENT_SECRET')
    }

    def before_get(self, parser, section, option, value, defaults):
        value = super(SecEnvInterpolation, self).before_get(parser, section, option, value, defaults)
        if option in self.env.keys() and self.env.get(option) is not None:
            return self.env.get(option)
        else:
            return value


def load_settings():
    cp = ConfigParser(interpolation=SecEnvInterpolation())
    root_dir = os.path.dirname(os.path.abspath(__file__))
    default_config_file = os.path.join(root_dir, 'defaults.cfg')
    config_file = os.path.join(root_dir, 'settings.cfg')
    cp.read_file(open(default_config_file))
    cp.read(config_file)
    return cp

settings = load_settings()

models_folder = "/tmp/colab/so-vits-svc-fork/models"

tenant = settings.get('DEFAULT', 'tenant', fallback='common')
authority_domain = settings.get('DEFAULT', 'authority_domain', fallback='https://login.microsoftonline.com')
authority_url = '{}/{}'.format(authority_domain, tenant)
auth_url = authority_url + '/oauth2/v2.0/authorize'
token_url = authority_url + '/oauth2/v2.0/token'
# drive_api = 'https://graph.microsoft.com/v1.0/me/drive'

client_id = settings.get('client_credentials', 'client_id', fallback='')
client_secret = settings.get('client_credentials', 'client_secret', fallback='')
redirect_uri = settings.get('client_credentials', 'redirect_uri', fallback='http://localhost')
# access_scopes = 'Files.Read Files.ReadWrite Files.Read.All Files.ReadWrite.All offline_access'
access_scopes = settings.get('client_credentials', 'access_scopes', fallback=store['all_scopes']).split(' ')

duration_limit = int(os.environ.get("MAX_DURATION_SECONDS", 9e9))
default_cluster_infer_ratio = 0.5

def generate_authorisation_url(session, client_id, access_scopes, redirect_uri='http://localhost', response_mode='query', tenant="common", auth_url=None):
    if not tenant:
        tenant="common"
    authority_url = f'{authority_domain}/{tenant}'
    if not auth_url:
        auth_url = f'{authority_url}/oauth2/v2.0/authorize'
    if not redirect_uri:
        redirect_uri = 'http://localhost'
    if not response_mode:
        response_mode = 'query'
    session['redirect_uri'] = redirect_uri
    params  = {}
    params['client_id'] = client_id
    params['redirect_uri'] = redirect_uri
    params['response_type'] = 'code'
    params['scope'] = access_scopes
    params['response_mode'] = response_mode
    return session, "{}?{}".format(auth_url, urllib.parse.urlencode(params))

def render_html(func):
    def wrapper(*args, **kwargs):
        url, session = func(*args, **kwargs)
        if url:
            html = f'<a href="{url}" target="_blank"><pre className="overflow-x-auto whitespace-pre-wrap p-2"><code>{url}</code></pre></a>'
            return html, session
        return url, session
    return wrapper

class OneDriveApp:
    def __init__(self, app: ClientApplication, scope=["https://graph.microsoft.com/.default"], **kwargs):
        self.app = app
        self.scope = scope
        self.clients = {}

    @property
    def accounts(self):
        self.get_accounts()

    def get_accounts(self, *args, **kwargs):
        global store
        accounts = self.app.get_accounts()
        if accounts:
            for account in accounts:
                # username = account["username"].split('@')[0]
                username = account["username"]
                result = self.app.acquire_token_silent(scopes=self.scope, account=account)
                if not result:
                    accounts.remove(account)
                    if username in store['accounts']:
                        store['accounts'].remove(username)
                    continue
                print(json.dumps(result, indent=4))
                if username not in store['accounts']:
                    store['accounts'].append(username)
            # store['accounts'] = [account["username"].split('@')[0] for account in accounts]
        return accounts

    def get_account(self, username, *args, **kwargs):
        if not username:
            return
        accounts = self.get_accounts()
        if accounts:
            for account in accounts:
                if username == account["username"]:
                    return account
        return None

    def get_client(self, username, *args, **kwargs):
        if not username:
            return
        client = self.clients.get(username)
        if not client:
            account = self.get_account(username)
            if not account:
                return None
            client = GraphClient(self.acquire_token_func(account))
            self.clients[username] = client
        return client

    def query_models(self, username, *args, **kwargs):
        if not username:
            return
        client = self.get_client(username)
        if not client:
            return
        global store
        if username not in store["models"].keys():
            store["models"][username] = {}
        models = {}
        models_items = client.me.drive.root.get_by_path(models_folder).children.get().execute_query()
        for model_item in models_items:  # type: DriveItem
            if model_item.name.startswith(".") or model_item.is_file:
                continue
            model_name = model_item.name
            models[model_name] = []
            if model_name not in store["models"][username].keys():
                store["models"][username][model_name] = []
            ckpts_items = client.me.drive.root.get_by_path(f"{models_folder}/{model_name}").children.get().execute_query()
            ckpts = []
            for ckpt_item in ckpts_items:  # type: DriveItem
                if ckpt_item.name.endswith(".pth") and ckpt_item.name.startswith("G_"):
                    ckpts.append(ckpt_item.name)
            p = re.compile(r'\d+')
            ckpts.sort(key=lambda s: int(p.search(s).group()), reverse=True)
            models[model_name] = ckpts
            store["models"][username][model_name] = ckpts
        print(json.dumps(models, indent=4))
        print(list(store["models"][username].keys()))
        return models

    def fetch_models(self, username, model, checkpoint, *args, **kwargs):
        if not username or not model or not checkpoint:
            return
        if not checkpoint.endswith(".json"):
            self.fetch_models(username, model, "config.json", *args, **kwargs)
        if os.path.exists(f"models/{username}/{model}/{checkpoint}") and os.path.isfile(f"models/{username}/{model}/{checkpoint}"):
            return True
        client = self.get_client(username)
        if not client:
            return
        def print_download_progress(offset):
            print("Downloaded '{0}' bytes...".format(offset))
        ckpt = client.me.drive.root.get_by_path(f"{models_folder}/{model}/{checkpoint}").get().execute_query()
        with tempfile.TemporaryDirectory() as local_path:
            with open(os.path.join(local_path, ckpt.name), 'wb') as local_file:
                ckpt.download_session(local_file, print_download_progress).execute_query()
            print("[Ok] File '{0}' has been downloaded into {1}".format(ckpt.name, local_file.name))
            os.path.exists(f"models/{username}/{model}") and os.path.isdir(f"models/{username}/{model}") or os.makedirs(f"models/{username}/{model}", exist_ok=True)
            shutil.move(local_file.name, f"models/{username}/{model}/{checkpoint}")
        return True

    # @render_html
    def initiate_flow(self, scopes=None, redirect_uri=None, session=None, *args, **kwargs):
        session = session or {}
        scopes = scopes or session.get("scopes") or store.get("scopes", self.scope)
        redirect_uri = redirect_uri or session.get("redirect_uri") or store.get("redirect_uri", "http://localhost")
        response_mode = session.get("response_mode") or store.get("response_mode", "query")
        if session.get("flow") and session.get("scopes") == scopes and session.get("redirect_uri") == redirect_uri and session.get("response_mode") == response_mode:
            return session.get("flow").get("auth_uri"), session
        flow = self.app.initiate_auth_code_flow(scopes=scopes, redirect_uri=redirect_uri, response_mode=response_mode)
        print(json.dumps(flow, indent=4))
        if "auth_uri" not in flow:
            # raise ValueError(
            #     "Fail to create auth code flow. Err: %s" % json.dumps(flow, indent=4))
            return None, session
        session["flow"] = flow
        session["scopes"] = scopes
        session["redirect_uri"] = redirect_uri
        session["response_mode"] = response_mode
        return flow.get("auth_uri"), session

    def acquire_token_by_flow(self, code, state=None, client_info=None, session_state=None, session=None, *args, **kwargs):
        session = session or {}
        flow = session.get("flow")
        if not flow:
            return None, session
        if session.get("redirect_uri") and code.startswith(session.get("redirect_uri")):
            params = urllib.parse.parse_qs(urllib.parse.urlparse(code).query)
            params = {k: v[0] for k, v in params.items()}
            code = params.get("code")
            state = params.get("state")
            client_info = params.get("client_info")
            session_state = params.get("session_state")
        params = { "code": code}
        if state:
            params["state"] = state
        if client_info:
            params["client_info"] = client_info
        if session_state:
            params["session_state"] = session_state
        token = self.app.acquire_token_by_auth_code_flow(flow, params)
        print(json.dumps(token, indent=4))
        if "access_token" not in token:
            return "Fail", session
        # global store
        self.flow = {}
        session["flow"] = {}
        session["token"] = token
        # store["token"] = result
        return token.get("access_token"), session

    def acquire_token_func(self, account):
        """
        Acquire token via MSAL
        """
        def acquire_token_silent():
            token = self.app.acquire_token_silent(scopes=self.scope, account=account)
            return token
        return acquire_token_silent

od_app = OneDriveApp(msal.ConfidentialClientApplication(
    authority=authority_url,
    client_id=client_id,
    client_credential=client_secret
))

class Model:
    def __init__(self):
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.model = None
        self.config_path = None
        self.model_path = None
        self.cluster_model_path = None
        self.hparams = None
        self.speakers = None
        self.cluster_infer_ratio = default_cluster_infer_ratio if self.cluster_model_path else 0

    def use_model(self, username, model, checkpoint, *args, **kwargs):
        if not username or not model or not checkpoint:
            return
        if not os.path.exists(f"models/{username}/{model}/{checkpoint}") or not os.path.isfile(f"models/{username}/{model}/{checkpoint}"):
            if not od_app.fetch_models(username, model, checkpoint, *args, **kwargs):
                return
        self.config_path = f"models/{username}/{model}/config.json"
        self.model_path = f"models/{username}/{model}/{checkpoint}"
        self.model = Svc(net_g_path=self.model_path,
            config_path=self.config_path, device=self.device, cluster_model_path=None)
        self.hparams = HParams(**json.loads(Path(f"models/{username}/{model}/config.json").read_text()))
        self.speakers = list(self.hparams.spk.keys())

model = Model()

def predict(
    speaker,
    audio,
    transpose: int = 0,
    auto_predict_f0: bool = False,
    cluster_infer_ratio: float = 0,
    noise_scale: float = 0.4,
    f0_method: str = "crepe",
    db_thresh: int = -40,
    pad_seconds: float = 0.5,
    chunk_seconds: float = 0.5,
    absolute_thresh: bool = False,
):
    audio, _ = librosa.load(audio, sr=model.model.target_sample, duration=duration_limit)
    audio = model.model.infer_silence(
        audio.astype(np.float32),
        speaker=speaker,
        transpose=transpose,
        auto_predict_f0=auto_predict_f0,
        cluster_infer_ratio=cluster_infer_ratio,
        noise_scale=noise_scale,
        f0_method=f0_method,
        db_thresh=db_thresh,
        pad_seconds=pad_seconds,
        chunk_seconds=chunk_seconds,
        absolute_thresh=absolute_thresh,
    )
    return model.model.target_sample, audio

with gr.Blocks(css="./style.css") as demo:
    session = gr.State({
        'client_id': client_id,
        'client_secret': client_secret,
        'redirect_uri': redirect_uri,
    })
    with gr.Row():
        accounts = gr.Dropdown(label="accounts", choices=store['accounts'])
        modules.ui.create_refresh_button(accounts, od_app.get_accounts, lambda: {"choices": store['accounts']}, 'refresh_accounts_states')
        models = gr.Dropdown(label="models")
        modules.ui.create_refresh_button(models, od_app.query_models, lambda a: {"choices": list(store["models"].get(a, {}).keys())} if a else {}, 'refresh_models_states', inputs=accounts)
        checkpoints = gr.Dropdown(label="checkpoints", allow_custom_value=True)
        modules.ui.create_refresh_button(checkpoints, od_app.query_models, lambda a, m: {"choices": store["models"].get(a, {}).get(m, [])} if a and m else {}, 'refresh_checkpoints_states', inputs=[accounts, models])
    # gr.Markdown("# <center> Flip text or image files using this demo.")
    # gr.HTML(value="<p style='margin-top: 1rem, margin-bottom: 1rem'>Gradio Docs Readers: <img src='http://visitor-badge.glitch.me/badge?page_id=gradio-docs-visitor-badge' alt='visitor badge' style='display: inline-block'/></p>")
    # gr.HTML(value='<a href="http://www.baidu.com">Upload  with huggingface_hub</a>')
    with gr.Tabs():
        with gr.Tab("Audio-To-Audio"):
            gr.Markdown("## <center> Welcome 🎉")
            with gr.Row():
                with gr.Column():
                    with gr.Tabs():
                        with gr.Tab("Clone From File"):
                            audio_file = gr.Audio(type="filepath", source="upload", label="Source Audio")
                        with gr.Tab("Clone From Mic"):
                            audio_mic = gr.Audio(type="filepath", source="microphone", label="Source Audio", interactive=False),
                    with gr.Row():
                        with gr.Column():
                            speaker = gr.Dropdown(label="speaker", choices=[0], allow_custom_value=True)
                        with gr.Column():
                            f0_method = gr.Dropdown(label="f0_method", choices=["crepe", "crepe-tiny", "parselmouth", "dio", "harvest"], value="crepe")
                        with gr.Column():
                            auto_predict_f0 = gr.Checkbox(False, label="auto_predict_f0")
                    transpose = gr.Slider(-12, 12, value=0, step=1, label="transpose")
                    with gr.Row():
                        cluster_infer_ratio = gr.Slider(0.0, 1.0, value=default_cluster_infer_ratio, step=0.1, label="cluster_infer_ratio")
                    with gr.Row():
                        noise_scale = gr.Slider(0.0, 1.0, value=0.4, step=0.1, label="noise_scale")
                    inference_btn = gr.Button("inference")
                with gr.Column():
                    output_audio = gr.Audio(label="output_audio")
        with gr.Tab("Login"):
            gr.Markdown("## <center> Welcome to your new drive 🎉")
            with gr.Box():
                # gr.Markdown("### Step 1/3: Preparations")
                with gr.Row():
                    client_id_input = gr.Textbox(label="client_id", placeholder="client_id", value=client_id)
                with gr.Row():
                    client_secret_input = gr.Textbox(label="client_secret", placeholder="client_secret", value=client_secret, type="password")
                with gr.Row():
                    scopes_input = gr.CheckboxGroup(label="scopes", choices=store.get('all_scopes', []), value=access_scopes)
                with gr.Row():
                    redirect_uri_input = gr.Textbox(label="redirect_uri", placeholder="redirect_uri", value=redirect_uri)
                # with gr.Row():
                #     auth_url_input = gr.Textbox(label="auth_url", placeholder="auth_url", value=auth_url)
                # with gr.Row():
                #     drive_api_input = gr.Textbox(label="drive_api", placeholder="drive_api", value=drive_api)
                # with gr.Row():
                #     with gr.Column():
                #         tenant_input = gr.Dropdown(label="tenant", choices=["common", "organizations", "consumers"], value=tenant, allow_custom_value=True)
                #     with gr.Column():
                #         response_mode_input = gr.Dropdown(label="response_mode", choices=["query", "form_post", "fragment"], value="query")
                with gr.Row():
                    with gr.Column():
                        clear_btn = gr.Button("Clear")
                    with gr.Column():
                        authorize_btn = gr.Button("Authorize")
                with gr.Row():
                    # auth_uri = gr.HTML(label="auth_uri")
                    auth_uri = gr.Textbox(label="auth_uri")
                    auth_uri.style(show_copy_button=True, container=True)
            with gr.Box():
                auth_code = gr.Textbox(label="auth_code", placeholder="M.R3_BAY.c0...")
                auth_state = gr.Textbox(label="auth_state", placeholder="pAfuWckRoda...")
                auth_client_info = gr.Textbox(label="auth_client_info", placeholder="eyJ1aWQiOiJ...")
                auth_session_state = gr.Textbox(label="auth_session_state", placeholder="dccc6b0f-a1...")
                with gr.Row():
                    with gr.Column():
                        get_token_btn = gr.Button("Get Token")
                with gr.Row():
                    token = gr.Textbox(label="token")

        accounts.select(
            fn=modules.ui.create_refresh_func(models, od_app.query_models, lambda a: {"choices": list(store["models"].get(a, {}).keys())} if a else {}),
            inputs=[accounts],
            outputs=[models],
        )
        models.select(
            fn=modules.ui.create_refresh_func(checkpoints, od_app.query_models, lambda a, m: {"choices": store["models"].get(a, {}).get(m, [])} if a and m else {}),
            inputs=[accounts, models],
            outputs=[checkpoints],
        )
        checkpoints.select(
            fn=modules.ui.create_refresh_func(checkpoints, model.use_model, {"choices": ["Kawashima_Kyoko"], "value": "Kawashima_Kyoko"}),
            inputs=[accounts, models, checkpoints],
            outputs=[speaker],
        )
        # inference_btn.click(
        #     fn=predict,
        #     inputs=[speaker, audio_file],
        #     outputs=[output_audio],
        # )
        # def acquire_token_by_flow_for_api(code, state=None, client_info=None, session_state=None):
        #     result, _ = od_app.acquire_token_by_flow(code, state, client_info, session_state)
        #     if result:
        #         return {"result": result}
        #     return {"result": "Fail"}
        authorize_btn.click(
            fn=od_app.initiate_flow,
            inputs=[scopes_input, redirect_uri_input, session],
            outputs=[auth_uri, session],
        )
        get_token_btn.click(
            fn=od_app.acquire_token_by_flow,
            inputs=[auth_code, auth_state, auth_client_info, auth_session_state, session],
            outputs=[token, session],
        )
        token.change(
            fn=modules.ui.create_refresh_func(accounts, od_app.get_accounts, lambda: {"choices": store['accounts']}),
            outputs=[accounts],
        )
        clear_btn.click(lambda: [None]*4, outputs=[client_id_input, client_secret_input, scopes_input, redirect_uri_input], queue=False)

if __name__ == "__main__":
    app, local_url, share_url = demo.launch(
        # prevent_thread_lock=True
    )
    # app.add_api_route("/oauth2/v2.0/token", acquire_token_by_flow_for_api)
    # while 1:
    #     time.sleep(1)

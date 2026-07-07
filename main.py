#!/usr/bin/env python3
"""
AIH REDDIT ANALYSER v3
Uses private Reddit session-based login + old Reddit JSON API.
"""

import asyncio
import base64
import json
import os
import random
import re
import threading
import time
import uuid
import tkinter as tk
from tkinter import ttk, scrolledtext, filedialog, messagebox
from datetime import datetime
import csv

from wreq import Client, Emulation, Proxy

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
SEC_CH_UA = '"Google Chrome";v="147", "Chromium";v="147", "Not)A;Brand";v="24"'
CLIENT_VERSION = "2026-06-08T21:07Z~0f806d93"
REDDIT_CAPTCHA_SITEKEY = "6LfirrMoAAAAAHZOipvza4kpp_VtTwLNuXVwURNQ"

CONFIG_DIR = os.path.expanduser("~/.zreddit_scraper")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")


def get_cookie_from_response(resp, name):
    for cookie in resp.cookies:
        if cookie.name == name:
            return cookie.value
    return None


async def _js_challenge(client):
    resp = await client.get("https://www.reddit.com/login/")
    html = await resp.text()
    token = re.search(r'name="token"\s+value="([^"]+)"', html).group(1)
    seed = re.search(r'await\(async e=>e\+e\)\("([^"]+)"\)', html).group(1)
    solution = seed + seed
    final_url = f"https://www.reddit.com/login/?solution={solution}&js_challenge=1&token={token}&jsc_orig_r="
    js_resp = await client.get(final_url)
    csrf_token = get_cookie_from_response(js_resp, "csrf_token") \
        or get_cookie_from_response(resp, "csrf_token")
    return final_url, csrf_token, js_resp


async def _solve_recaptcha(api_key, proxy_url):
    cap = Client(emulation=Emulation.Chrome147)
    task = {
        "type": "ReCaptchaV3EnterpriseToken",
        "websiteURL": "https://www.reddit.com/login/",
        "websiteKey": REDDIT_CAPTCHA_SITEKEY,
        "pageTitle": "reddit.com",
        "pageAction": "v1/web/login_with_password",
        "minScore": 0.9,
        "apiDomain": "google.com",
        "isSession": True,
        "proxy": proxy_url,
    }
    body = {"clientKey": api_key, "task": task, "provider": "CapSolver"}
    create = await cap.post("https://api.anysolver.com/createTask",
        headers={"Content-Type": "application/json"}, json=body)
    cdata = await create.json()
    if cdata.get("errorId") not in (0, "0", "SUCCESS"):
        raise RuntimeError(f"createTask failed: {cdata}")
    task_id = cdata["taskId"]
    await asyncio.sleep(4)
    for _ in range(40):
        poll = await cap.post("https://api.anysolver.com/getTaskResult",
            headers={"Content-Type": "application/json"},
            json={"clientKey": api_key, "taskId": task_id})
        result = await poll.json()
        if result.get("status") == "ready":
            return result.get("solution", {}).get("token", "")
        if result.get("status") == "failed":
            raise RuntimeError(f"solve failed: {result}")
        await asyncio.sleep(3)
    raise RuntimeError("solve timed out")


async def _login_flow(client, username, password, proxy_url, solver_key):
    final_url, csrf_token, js_resp = await _js_challenge(client)
    rc_key = base64.urlsafe_b64encode(f"login|initial|{uuid.uuid4()}".encode()).decode().rstrip("=")
    await client.get(f"https://www.reddit.com/svc/shreddit/update-recaptcha?k={rc_key}",
        headers={"accept": "*/*", "referer": final_url, "user-agent": UA,
                 "sec-fetch-dest": "empty", "sec-fetch-mode": "cors",
                 "sec-fetch-site": "same-origin"})
    js_html = await js_resp.text()
    m = re.search(r"LoginStep_([A-Za-z0-9]+)", js_html)
    if m:
        await client.get(
            f"https://www.reddit.com/svc/shreddit/partial/{m.group(1)}/login-step?is_standalone=true&source_page=login",
            headers={"accept": "text/vnd.reddit.partial+html, text/html;q=0.9",
                     "content-type": "application/x-www-form-urlencoded",
                     "referer": final_url, "user-agent": UA,
                     "sec-fetch-dest": "empty", "sec-fetch-mode": "cors",
                     "sec-fetch-site": "same-origin",
                     "x-original-referer": "https://www.reddit.com/login/",
                     "x-reddit-client-version": CLIENT_VERSION})
    await client.post(
        "https://www.reddit.com/svc/shreddit/account/login/check_is_oidc_required",
        headers={"accept": "application/json", "content-type": "application/json",
                 "origin": "https://www.reddit.com", "referer": final_url,
                 "user-agent": UA, "x-reddit-client-version": CLIENT_VERSION},
        json={"userIdentifier": username, "csrf_token": csrf_token})
    recaptcha_token = await _solve_recaptcha(solver_key, proxy_url)
    login_headers = {
        "accept": "text/vnd.reddit.partial+html, application/json",
        "accept-language": "en-US,en;q=0.9",
        "cache-control": "no-cache",
        "origin": "https://www.reddit.com",
        "pragma": "no-cache",
        "priority": "u=1, i",
        "referer": final_url,
        "sec-ch-ua": SEC_CH_UA,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": UA,
        "x-original-referer": "https://www.reddit.com/login/",
        "x-reddit-client-version": CLIENT_VERSION,
    }
    form_data = {
        "username": username,
        "password": password,
        "recaptcha_token": recaptcha_token,
        "recaptcha_use_checkbox": "false",
        "recaptcha_action": "v1/web/login_with_password",
        "csrf_token": csrf_token,
    }
    resp = await client.post("https://www.reddit.com/svc/shreddit/account/login",
        headers=login_headers, form=form_data)
    resp_text = await resp.text()
    session_val = get_cookie_from_response(resp, "reddit_session")
    if session_val:
        return True
    if "WRONG_PASSWORD" in resp_text:
        return False
    return False


async def _fetch_subreddit_data(client, subreddit_name, num_posts):
    url = f"https://old.reddit.com/r/{subreddit_name}/new/.json?limit={num_posts}&raw_json=1"
    resp = await client.get(url, headers={"user-agent": UA})
    data = await resp.json()
    children = data.get("data", {}).get("children", [])
    posts = []
    for c in children:
        d = c["data"]
        posts.append({
            "author": d.get("author", ""),
            "title": d.get("title", ""),
            "id": d.get("id", ""),
            "created_utc": d.get("created_utc", 0),
        })
    return posts


async def _fetch_author_info(client, author_name):
    if not author_name or author_name == "[deleted]":
        return {"link_karma": 0, "comment_karma": 0, "created_utc": 0}
    try:
        resp = await client.get(
            f"https://old.reddit.com/user/{author_name}/about.json?raw_json=1",
            headers={"user-agent": UA})
        d = (await resp.json()).get("data", {})
        return {
            "link_karma": d.get("link_karma", 0),
            "comment_karma": d.get("comment_karma", 0),
            "created_utc": d.get("created_utc", 0),
        }
    except Exception:
        return {"link_karma": 0, "comment_karma": 0, "created_utc": 0}


async def _fetch_user_profile(client, username):
    profile = {"username": username, "exists": False}
    resp = await client.get(
        f"https://old.reddit.com/user/{username}/about.json?raw_json=1",
        headers={"user-agent": UA})
    if resp.status != 200:
        return profile
    d = (await resp.json()).get("data", {})
    if not d or d.get("name") is None:
        return profile
    profile["exists"] = True
    profile["created_utc"] = d.get("created_utc", 0)
    profile["post_karma"] = d.get("link_karma", 0)
    profile["comment_karma"] = d.get("comment_karma", 0)
    profile["post_subreddits"] = set()
    profile["comment_subreddits"] = set()

    submitted_resp = await client.get(
        f"https://old.reddit.com/user/{username}/submitted/.json?limit=100&raw_json=1",
        headers={"user-agent": UA})
    submitted_data = (await submitted_resp.json()).get("data", {}).get("children", [])
    for c in submitted_data:
        sr = c["data"].get("subreddit", "")
        if sr:
            profile["post_subreddits"].add(sr.lower())
    profile["post_count"] = len(submitted_data)

    comments_resp = await client.get(
        f"https://old.reddit.com/user/{username}/comments/.json?limit=100&raw_json=1",
        headers={"user-agent": UA})
    comments_data = (await comments_resp.json()).get("data", {}).get("children", [])
    for c in comments_data:
        sr = c["data"].get("subreddit", "")
        if sr:
            profile["comment_subreddits"].add(sr.lower())
    profile["comment_count"] = len(comments_data)
    return profile


class ZRedditScraperGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("AIH REDDIT ANALYSER")
        self.root.geometry("900x700")
        self.root.minsize(800, 600)

        self.client = None
        self.authenticated = False
        self._settings_configured = False

        self.num_posts = tk.IntVar(value=5)
        self.show_detailed = tk.BooleanVar(value=False)
        self.status_message = tk.StringVar(value="Step 1: Configure settings")

        self.username_var = tk.StringVar()
        self.password_var = tk.StringVar()
        self.proxy_file_var = tk.StringVar()
        self.solver_key_var = tk.StringVar()
        self.current_user_data = {}
        self.proxy_list = []

        self.load_config()
        self.create_ui()
        self._apply_tab_states()

    def load_config(self):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r") as f:
                    cfg = json.load(f)
                self.num_posts.set(cfg.get("num_posts", 5))
                self.show_detailed.set(cfg.get("show_detailed", False))
                self.proxy_file_var.set(cfg.get("proxy_file", ""))
                self.solver_key_var.set(cfg.get("solver_key", ""))
                if self.proxy_file_var.get() and self.solver_key_var.get():
                    self._settings_configured = True
                    self._load_proxy_list()
            except Exception:
                pass

    def save_config(self):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        cfg = {
            "num_posts": self.num_posts.get(),
            "show_detailed": self.show_detailed.get(),
            "proxy_file": self.proxy_file_var.get(),
            "solver_key": self.solver_key_var.get(),
        }
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f)

    def _load_proxy_list(self):
        path = self.proxy_file_var.get().strip()
        if not path or not os.path.exists(path):
            self.proxy_list = []
            return
        try:
            with open(path, "r") as f:
                self.proxy_list = [
                    line.strip() for line in f
                    if line.strip() and not line.strip().startswith("#")
                ]
        except Exception:
            self.proxy_list = []

    def _get_random_proxy(self):
        if self.proxy_list:
            return random.choice(self.proxy_list)
        return None

    def _apply_tab_states(self):
        settings_idx, auth_idx, sub_idx, user_idx = 0, 1, 2, 3
        self.notebook.tab(settings_idx, state="normal")
        if not self._settings_configured:
            self.notebook.tab(auth_idx, state="disabled")
            self.notebook.tab(sub_idx, state="disabled")
            self.notebook.tab(user_idx, state="disabled")
        elif not self.authenticated:
            self.notebook.tab(auth_idx, state="normal")
            self.notebook.tab(sub_idx, state="disabled")
            self.notebook.tab(user_idx, state="disabled")
        else:
            self.notebook.tab(auth_idx, state="normal")
            self.notebook.tab(sub_idx, state="normal")
            self.notebook.tab(user_idx, state="normal")

    def create_ui(self):
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.pack(fill=tk.BOTH, expand=True)
        title_frame = ttk.Frame(main_frame)
        title_frame.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(title_frame, text="AIH REDDIT ANALYSER",
                  font=("Helvetica", 16, "bold")).pack(side=tk.LEFT)

        self.notebook = ttk.Notebook(main_frame)
        self.notebook.pack(fill=tk.BOTH, expand=True)

        self.settings_tab = ttk.Frame(self.notebook, padding=10)
        self.auth_tab = ttk.Frame(self.notebook, padding=10)
        self.subreddit_tab = ttk.Frame(self.notebook, padding=10)
        self.user_tab = ttk.Frame(self.notebook, padding=10)
        self.about_tab = ttk.Frame(self.notebook, padding=10)

        self.notebook.add(self.settings_tab, text="Settings")
        self.notebook.add(self.auth_tab, text="Authentication")
        self.notebook.add(self.subreddit_tab, text="Subreddit Analysis")
        self.notebook.add(self.user_tab, text="User Analysis")
        self.notebook.add(self.about_tab, text="About")

        self.create_settings_tab()
        self.create_auth_tab()
        self.create_subreddit_tab()
        self.create_user_tab()
        self.create_about_tab()

        status_bar = ttk.Frame(main_frame)
        status_bar.pack(fill=tk.X, pady=(10, 0))
        ttk.Label(status_bar, textvariable=self.status_message).pack(side=tk.LEFT)
        self.progress = ttk.Progressbar(status_bar, mode="indeterminate")
        self.progress.pack(side=tk.RIGHT, fill=tk.X, expand=True)

    def create_settings_tab(self):
        header = ttk.Label(self.settings_tab,
            text="Configure your settings before proceeding.",
            font=("Helvetica", 11, "bold"), wraplength=600)
        header.pack(fill=tk.X, pady=(0, 5))
        note = ttk.Label(self.settings_tab,
            text="For best results, use static residential proxies.",
            foreground="gray", wraplength=600)
        note.pack(fill=tk.X, pady=(0, 15))

        form_frame = ttk.Frame(self.settings_tab)
        form_frame.pack(fill=tk.X, pady=10)

        ttk.Label(form_frame, text="Proxies File (proxies.txt):").grid(
            row=0, column=0, sticky=tk.W, pady=5)
        pf = ttk.Frame(form_frame)
        pf.grid(row=0, column=1, sticky=tk.W, pady=5, padx=(10, 0))
        ttk.Entry(pf, textvariable=self.proxy_file_var, width=40).pack(side=tk.LEFT)
        ttk.Button(pf, text="Browse", command=self._browse_proxy_file).pack(
            side=tk.LEFT, padx=(5, 0))

        ttk.Label(form_frame, text="AnySolver API Key:").grid(
            row=1, column=0, sticky=tk.W, pady=5)
        ttk.Entry(form_frame, textvariable=self.solver_key_var, width=45, show="*").grid(
            row=1, column=1, sticky=tk.W, pady=5, padx=(10, 0))

        ttk.Label(form_frame, text="Number of posts to analyze (1-100):").grid(
            row=2, column=0, sticky=tk.W, pady=5)
        ttk.Spinbox(form_frame, from_=1, to=100, width=5,
                    textvariable=self.num_posts).grid(
            row=2, column=1, sticky=tk.W, pady=5, padx=(10, 0))

        ttk.Checkbutton(form_frame, text="Show detailed user data",
                        variable=self.show_detailed).grid(
            row=3, column=1, sticky=tk.W, pady=5, padx=(10, 0))

        ttk.Button(form_frame, text="Save Settings",
                   command=self.save_settings).grid(
            row=4, column=0, columnspan=2, pady=(20, 0))

        self.settings_status_var = tk.StringVar(value="")
        ttk.Label(form_frame, textvariable=self.settings_status_var,
                  foreground="green").grid(
            row=5, column=0, columnspan=2, pady=(10, 0))

    def _browse_proxy_file(self):
        path = filedialog.askopenfilename(
            title="Select proxies.txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")])
        if path:
            self.proxy_file_var.set(path)

    def create_auth_tab(self):
        instructions = ttk.Label(self.auth_tab,
            text="Log in with your Reddit account to begin analysis.",
            wraplength=600, justify=tk.LEFT)
        instructions.pack(fill=tk.X, pady=(0, 10))
        form_frame = ttk.Frame(self.auth_tab)
        form_frame.pack(fill=tk.X, pady=10)
        ttk.Label(form_frame, text="Username / Email:").grid(
            row=0, column=0, sticky=tk.W, pady=5)
        self.username_entry = ttk.Entry(
            form_frame, textvariable=self.username_var, width=45)
        self.username_entry.grid(row=0, column=1, sticky=tk.W, pady=5, padx=(10, 0))
        ttk.Label(form_frame, text="Password:").grid(
            row=1, column=0, sticky=tk.W, pady=5)
        self.password_entry = ttk.Entry(
            form_frame, textvariable=self.password_var, width=45, show="*")
        self.password_entry.grid(row=1, column=1, sticky=tk.W, pady=5, padx=(10, 0))
        self.auth_button = ttk.Button(form_frame, text="Login",
                                       command=self.authenticate_reddit)
        self.auth_button.grid(row=2, column=0, columnspan=2, pady=(20, 0))
        self.auth_status_var = tk.StringVar(value="Not logged in")
        ttk.Label(form_frame, textvariable=self.auth_status_var).grid(
            row=3, column=0, columnspan=2, pady=(10, 0))

    def create_subreddit_tab(self):
        input_frame = ttk.Frame(self.subreddit_tab)
        input_frame.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(input_frame, text="Subreddit name (without r/):").pack(
            side=tk.LEFT, padx=(0, 5))
        self.subreddit_entry = ttk.Entry(input_frame, width=30)
        self.subreddit_entry.pack(side=tk.LEFT, padx=(0, 10))
        ttk.Button(input_frame, text="Analyze Subreddit",
                   command=self.analyze_subreddit).pack(side=tk.LEFT)
        result_frame = ttk.LabelFrame(self.subreddit_tab, text="Analysis Results")
        result_frame.pack(fill=tk.BOTH, expand=True)
        self.subreddit_result_text = scrolledtext.ScrolledText(
            result_frame, wrap=tk.WORD, width=40, height=10)
        self.subreddit_result_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        self.subreddit_result_text.config(state=tk.DISABLED)

    def create_user_tab(self):
        input_frame = ttk.Frame(self.user_tab)
        input_frame.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(input_frame, text="Username (without u/):").pack(
            side=tk.LEFT, padx=(0, 5))
        self.user_analysis_entry = ttk.Entry(input_frame, width=30)
        self.user_analysis_entry.pack(side=tk.LEFT, padx=(0, 10))
        ttk.Button(input_frame, text="Analyze User",
                   command=self.analyze_user).pack(side=tk.LEFT)
        result_frame = ttk.LabelFrame(self.user_tab, text="Analysis Results")
        result_frame.pack(fill=tk.BOTH, expand=True)
        self.user_result_text = scrolledtext.ScrolledText(
            result_frame, wrap=tk.WORD, width=40, height=10)
        self.user_result_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        self.user_result_text.config(state=tk.DISABLED)
        action_frame = ttk.Frame(self.user_tab)
        action_frame.pack(fill=tk.X, pady=(10, 0))
        self.export_csv_button = ttk.Button(action_frame, text="Export to CSV",
                                            command=self.export_user_analysis,
                                            state=tk.DISABLED)
        self.export_csv_button.pack(side=tk.LEFT)

    def create_about_tab(self):
        about_frame = ttk.Frame(self.about_tab)
        about_frame.pack(fill=tk.BOTH, expand=True, pady=20, padx=20)
        ttk.Label(about_frame, text="AIH REDDIT ANALYSER",
                  font=("Helvetica", 16, "bold")).pack(pady=(0, 10))
        ttk.Label(about_frame, text="AIH REDDIT ANALYSER v3.0").pack()
        ttk.Label(about_frame, text="AIH NETWORK",
                  font=("Helvetica", 12)).pack(pady=(20, 5))
        desc = (
            "This tool analyzes Reddit subreddits and user profiles.\n"
            "Determines minimum posting requirements and user activity.\n\n"
            "Features:\n"
            " - Subreddit analysis: min karma & account age requirements\n"
            " - User analysis: active subreddits, karma breakdown\n"
            " - Export data to CSV\n\n"
            "Uses private Reddit session login + old Reddit JSON API."
        )
        ttk.Label(about_frame, text=desc, wraplength=600,
                  justify=tk.CENTER).pack(pady=20)

    def save_settings(self):
        proxy_file = self.proxy_file_var.get().strip()
        solver_key = self.solver_key_var.get().strip()
        if not proxy_file:
            messagebox.showerror("Error", "Please select a proxies.txt file")
            return
        if not solver_key:
            messagebox.showerror("Error", "Please enter your AnySolver API key")
            return
        if not os.path.exists(proxy_file):
            messagebox.showerror("Error", f"Proxy file not found:\n{proxy_file}")
            return
        self._load_proxy_list()
        if not self.proxy_list:
            messagebox.showerror("Error", "No valid proxies found in file")
            return
        self._settings_configured = True
        self.save_config()
        self._apply_tab_states()
        self.settings_status_var.set("Settings saved successfully")
        self.status_message.set("Step 2: Login with your Reddit account")
        self.root.after(3000, lambda: self.settings_status_var.set(""))

    def authenticate_reddit(self):
        if not self._settings_configured:
            messagebox.showerror("Error", "Please configure settings first")
            self.notebook.select(0)
            return
        username = self.username_var.get().strip()
        password = self.password_var.get().strip()
        if not username or not password:
            messagebox.showerror("Error", "Please enter username/email and password")
            return
        self.status_message.set("Logging in...")
        self.auth_status_var.set("Authenticating...")
        self.auth_button.config(state=tk.DISABLED)
        self.username_entry.config(state=tk.DISABLED)
        self.password_entry.config(state=tk.DISABLED)
        self.progress.start()
        self.root.update_idletasks()
        t = threading.Thread(target=self._auth_thread,
                             args=(username, password), daemon=True)
        t.start()

    def _auth_thread(self, username, password):
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            proxy_url = self._get_random_proxy()
            solver_key = self.solver_key_var.get().strip()
            proxies = [Proxy.all(proxy_url)] if proxy_url else None
            client = Client(emulation=Emulation.Chrome147, proxies=proxies, cookie_store=True)
            ok = loop.run_until_complete(
                _login_flow(client, username, password, proxy_url, solver_key))
            if ok:
                self.client = client
                self.authenticated = True
                self.root.after(0, self._apply_tab_states)
                self.root.after(0, lambda: self.auth_status_var.set(
                    f"Logged in as {username}"))
                self.root.after(0, lambda: self.status_message.set(
                    "Ready. You can now analyze subreddits and users."))
            else:
                self.root.after(0, lambda: self.auth_status_var.set(
                    "Login failed - check credentials"))
                self.root.after(0, lambda: self.status_message.set("Login failed"))
                self.root.after(0, lambda: messagebox.showerror("Login Failed",
                    "Login failed. Check your credentials."))
        except Exception as e:
            self.root.after(0, lambda: self.auth_status_var.set(
                f"Error: {str(e)[:50]}"))
            self.root.after(0, lambda: self.status_message.set(f"Auth error: {e}"))
            self.root.after(0, lambda: messagebox.showerror("Auth Error", str(e)))
        finally:
            self.root.after(0, self.progress.stop)
            self.root.after(0, lambda: self.auth_button.config(state=tk.NORMAL))
            self.root.after(0, lambda: self.username_entry.config(state=tk.NORMAL))
            self.root.after(0, lambda: self.password_entry.config(state=tk.NORMAL))

    def analyze_subreddit(self):
        if not self.authenticated:
            messagebox.showerror("Error", "Please login first")
            self.notebook.select(1)
            return
        subreddit_name = self.subreddit_entry.get().strip()
        if not subreddit_name:
            messagebox.showerror("Error", "Please enter a subreddit name")
            return
        self.subreddit_result_text.config(state=tk.NORMAL)
        self.subreddit_result_text.delete(1.0, tk.END)
        self.subreddit_result_text.config(state=tk.DISABLED)
        self.status_message.set(f"Analyzing subreddit r/{subreddit_name}...")
        self.progress.start()
        t = threading.Thread(target=self._analyze_subreddit_thread,
                             args=(subreddit_name,), daemon=True)
        t.start()

    def _analyze_subreddit_thread(self, subreddit_name):
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            num_posts = self.num_posts.get()
            posts = loop.run_until_complete(
                _fetch_subreddit_data(self.client, subreddit_name, num_posts))
            if not posts:
                self.root.after(0, lambda: self._update_subreddit_result(
                    f"No posts found in r/{subreddit_name}"))
                return
            seen = set()
            user_data = {}
            unique_authors = [p["author"] for p in posts
                              if p.get("author") and p["author"] != "[deleted]"]
            total_authors = len(set(unique_authors))
            self.root.after(0, lambda: self.status_message.set(
                f"Fetching author profiles ({total_authors} users)..."))
            for post in posts:
                author = post.get("author", "")
                if not author or author == "[deleted]" or author in seen:
                    continue
                seen.add(author)
                info = loop.run_until_complete(
                    _fetch_author_info(self.client, author))
                created_utc = info.get("created_utc", 0)
                age_days = (datetime.now().timestamp() - created_utc) / 86400 \
                    if created_utc else 9999
                user_data[author] = {
                    "age_days": age_days,
                    "age_formatted": self._format_time_delta(age_days),
                    "post_karma": info.get("link_karma", 0),
                    "comment_karma": info.get("comment_karma", 0),
                    "combined_karma": info.get("link_karma", 0) + info.get("comment_karma", 0),
                }
                if len(user_data) % 5 == 0:
                    time.sleep(0.3)
            if user_data:
                ages = [d["age_days"] for d in user_data.values()]
                pk = [d["post_karma"] for d in user_data.values()]
                ck = [d["comment_karma"] for d in user_data.values()]
                comb = [d["combined_karma"] for d in user_data.values()]
                lowest_user = min(user_data.items(), key=lambda x: x[1]["combined_karma"])
                lu_name, lu_data = lowest_user
                result = f"===== SUBREDDIT ANALYSIS: r/{subreddit_name} =====\n\n"
                result += f"Based on {len(user_data)} unique users from {len(posts)} recent posts.\n\n"
                result += "Minimum values observed (likely requirements):\n"
                result += f"Account Age: {self._format_time_delta(min(ages))}\n"
                result += f"Post Karma: {min(pk)}\n"
                result += f"Comment Karma: {min(ck)}\n"
                result += f"Combined Karma: {min(comb)}\n\n"
                result += "Lowest karma user details:\n"
                result += f"Username: u/{lu_name}\n"
                result += f"Account Age: {lu_data['age_formatted']}\n"
                result += f"Post Karma: {lu_data['post_karma']}\n"
                result += f"Comment Karma: {lu_data['comment_karma']}\n"
                result += f"Combined Karma: {lu_data['combined_karma']}\n\n"
                result += "Note: Estimates based on recent successful posts.\n"
                if self.show_detailed.get():
                    result += "\n===== DETAILED USER DATA =====\n\n"
                    for uname, d in user_data.items():
                        result += f"u/{uname} | Age: {d['age_formatted']} | "
                        result += f"Post: {d['post_karma']} | Comment: {d['comment_karma']}\n"
                self.root.after(0, lambda: self._update_subreddit_result(result))
                self.root.after(0, lambda: self.status_message.set(
                    f"Analysis of r/{subreddit_name} completed"))
            else:
                self.root.after(0, lambda: self._update_subreddit_result(
                    f"No valid user data collected from r/{subreddit_name}"))
        except Exception as e:
            self.root.after(0, lambda: self._update_subreddit_result(f"Error: {e}"))
            self.root.after(0, lambda: self.status_message.set(f"Error: {e}"))
        finally:
            self.root.after(0, self.progress.stop)

    def _update_subreddit_result(self, text):
        self.subreddit_result_text.config(state=tk.NORMAL)
        self.subreddit_result_text.delete(1.0, tk.END)
        self.subreddit_result_text.insert(tk.END, text)
        self.subreddit_result_text.config(state=tk.DISABLED)

    def analyze_user(self):
        if not self.authenticated:
            messagebox.showerror("Error", "Please login first")
            self.notebook.select(1)
            return
        username = self.user_analysis_entry.get().strip()
        if not username:
            messagebox.showerror("Error", "Please enter a username")
            return
        self.user_result_text.config(state=tk.NORMAL)
        self.user_result_text.delete(1.0, tk.END)
        self.user_result_text.config(state=tk.DISABLED)
        self.export_csv_button.config(state=tk.DISABLED)
        self.status_message.set(f"Analyzing user u/{username}...")
        self.progress.start()
        t = threading.Thread(target=self._analyze_user_thread,
                             args=(username,), daemon=True)
        t.start()

    def _analyze_user_thread(self, username):
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            profile = loop.run_until_complete(
                _fetch_user_profile(self.client, username))
            if not profile.get("exists"):
                self.root.after(0, lambda: self._update_user_result(
                    f"User u/{username} not found or profile is private."))
                return
            created_utc = profile.get("created_utc", 0)
            age_days = (datetime.now().timestamp() - created_utc) / 86400 \
                if created_utc else 0
            self.current_user_data = {
                "username": username,
                "account_created": datetime.fromtimestamp(created_utc)
                    if created_utc else datetime.now(),
                "account_age_days": age_days,
                "post_karma": profile.get("post_karma", 0),
                "comment_karma": profile.get("comment_karma", 0),
                "post_subreddits": profile.get("post_subreddits", set()),
                "comment_subreddits": profile.get("comment_subreddits", set()),
                "post_count": profile.get("post_count", 0),
                "comment_count": profile.get("comment_count", 0),
            }
            all_srs = (self.current_user_data["post_subreddits"] |
                       self.current_user_data["comment_subreddits"])
            result = f"===== USER ANALYSIS: u/{username} =====\n\n"
            if created_utc:
                result += f"Account created: {datetime.fromtimestamp(created_utc).strftime('%Y-%m-%d')}\n"
            result += f"Account age: {self._format_time_delta(age_days)}\n"
            result += f"Post karma: {self.current_user_data['post_karma']}\n"
            result += f"Comment karma: {self.current_user_data['comment_karma']}\n"
            result += f"Total karma: {self.current_user_data['post_karma'] + self.current_user_data['comment_karma']}\n\n"
            result += f"Posts analyzed: {self.current_user_data['post_count']}\n"
            result += f"Comments analyzed: {self.current_user_data['comment_count']}\n\n"
            result += "Subreddit Interaction Summary:\n"
            result += f"- Posted in: {len(self.current_user_data['post_subreddits'])} subreddits\n"
            result += f"- Commented in: {len(self.current_user_data['comment_subreddits'])} subreddits\n"
            result += f"- Total unique: {len(all_srs)} subreddits\n\n"
            if self.show_detailed.get():
                if self.current_user_data["post_subreddits"]:
                    result += "Posted in:\n"
                    for sr in sorted(self.current_user_data["post_subreddits"]):
                        result += f"- r/{sr}\n"
                    result += "\n"
                if self.current_user_data["comment_subreddits"]:
                    result += "Commented in:\n"
                    for sr in sorted(self.current_user_data["comment_subreddits"]):
                        result += f"- r/{sr}\n"
                    result += "\n"
            self.root.after(0, lambda: self._update_user_result(result))
            self.root.after(0, lambda: self.export_csv_button.config(state=tk.NORMAL))
            self.root.after(0, lambda: self.status_message.set(
                f"Analysis of u/{username} completed"))
        except Exception as e:
            self.root.after(0, lambda: self._update_user_result(
                f"Error analyzing user: {e}"))
            self.root.after(0, lambda: self.status_message.set(f"Error: {e}"))
        finally:
            self.root.after(0, self.progress.stop)

    def _update_user_result(self, text, append=False):
        self.user_result_text.config(state=tk.NORMAL)
        if not append:
            self.user_result_text.delete(1.0, tk.END)
        self.user_result_text.insert(tk.END, text)
        self.user_result_text.config(state=tk.DISABLED)

    def export_user_analysis(self):
        if not self.current_user_data:
            messagebox.showerror("Error", "No user data available for export")
            return
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        default_fn = f"user_analysis_{self.current_user_data['username']}_{timestamp}.csv"
        filename = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
            initialfile=default_fn)
        if not filename:
            return
        try:
            with open(filename, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                d = self.current_user_data
                w.writerow(["User Analysis Report"])
                w.writerow(["Generated on", datetime.now().strftime("%Y-%m-%d %H:%M:%S")])
                w.writerow([])
                w.writerow(["Username", d["username"]])
                w.writerow(["Account Created", d["account_created"].strftime("%Y-%m-%d")])
                w.writerow(["Account Age", self._format_time_delta(d["account_age_days"])])
                w.writerow(["Post Karma", d["post_karma"]])
                w.writerow(["Comment Karma", d["comment_karma"]])
                w.writerow(["Total Karma", d["post_karma"] + d["comment_karma"]])
                w.writerow([])
                w.writerow(["Activity Summary"])
                w.writerow(["Posts Analyzed", d["post_count"]])
                w.writerow(["Comments Analyzed", d["comment_count"]])
                all_srs = d["post_subreddits"] | d["comment_subreddits"]
                w.writerow(["Total Unique Subreddits", len(all_srs)])
                w.writerow([])
                w.writerow(["Subreddits Active In"])
                for sr in sorted(d["post_subreddits"] | d["comment_subreddits"]):
                    w.writerow([sr])
            self.status_message.set(f"Exported to {filename}")
            messagebox.showinfo("Export Complete", f"Exported to {filename}")
        except Exception as e:
            messagebox.showerror("Export Error", str(e))

    def _format_time_delta(self, days):
        if days < 1:
            return f"{days * 24:.1f} hours"
        elif days < 30:
            return f"{days:.1f} days"
        elif days < 365:
            return f"{days / 30.44:.1f} months"
        else:
            return f"{days / 365.25:.1f} years"


def main():
    root = tk.Tk()
    app = ZRedditScraperGUI(root)
    root.protocol("WM_DELETE_WINDOW", lambda: (app.save_config(), root.destroy()))
    root.mainloop()


if __name__ == "__main__":
    main()

"""
Base Vertex, Google AI Studio LLM Class

Handles Authentication and generating request urls for Vertex AI and Google AI Studio
"""

import json
import os
from typing import TYPE_CHECKING, Any, Literal, Optional, Tuple

from litellm._logging import verbose_logger
from litellm.litellm_core_utils.asyncify import asyncify
from litellm.llms.base import BaseLLM
from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler
from litellm.types.llms.vertex_ai import VERTEX_CREDENTIALS_TYPES

from .common_utils import _get_gemini_url, _get_vertex_url, all_gemini_url_modes

if TYPE_CHECKING:
    from google.auth.credentials import Credentials as GoogleCredentialsObject
else:
    GoogleCredentialsObject = Any


class VertexBase(BaseLLM):
    def __init__(self) -> None:
        super().__init__()
        self.access_token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self._credentials: Optional[GoogleCredentialsObject] = None
        self.project_id: Optional[str] = None
        self.async_handler: Optional[AsyncHTTPHandler] = None

    def get_vertex_region(self, vertex_region: Optional[str]) -> str:
        return vertex_region or "us-central1"

    def load_auth(
        self, credentials: Optional[VERTEX_CREDENTIALS_TYPES], project_id: Optional[str]
    ) -> Tuple[Any, str]:
        import google.auth as google_auth
        from google.auth import identity_pool
        from google.auth.transport.requests import (
            Request,  # type: ignore[import-untyped]
        )

        if credentials is not None:
            import google.oauth2.service_account

            if isinstance(credentials, str):
                verbose_logger.debug(
                    "Vertex: Loading vertex credentials from %s", credentials
                )
                verbose_logger.debug(
                    "Vertex: checking if credentials is a valid path, os.path.exists(%s)=%s, current dir %s",
                    credentials,
                    os.path.exists(credentials),
                    os.getcwd(),
                )

                try:
                    if os.path.exists(credentials):
                        json_obj = json.load(open(credentials))
                    else:
                        json_obj = json.loads(credentials)
                except Exception:
                    raise Exception(
                        "Unable to load vertex credentials from environment. Got={}".format(
                            credentials
                        )
                    )
            elif isinstance(credentials, dict):
                json_obj = credentials
            else:
                raise ValueError(
                    "Invalid credentials type: {}".format(type(credentials))
                )

            # Check if the JSON object contains Workload Identity Federation configuration
            if "type" in json_obj and json_obj["type"] == "external_account":
                creds = identity_pool.Credentials.from_info(json_obj)
            else:
                creds = (
                    google.oauth2.service_account.Credentials.from_service_account_info(
                        json_obj,
                        scopes=["https://www.googleapis.com/auth/cloud-platform"],
                    )
                )

            if project_id is None:
                project_id = getattr(creds, "project_id", None)
        else:
            creds, creds_project_id = google_auth.default(
                quota_project_id=project_id,
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
            if project_id is None:
                project_id = creds_project_id or creds.quota_project_id

        creds.refresh(Request())  # type: ignore

        if not project_id:
            raise ValueError("Could not resolve project_id")

        if not isinstance(project_id, str):
            raise TypeError(
                f"Expected project_id to be a str but got {type(project_id)}"
            )

        return creds, project_id

    def refresh_auth(self, credentials: Any) -> None:
        from google.auth.transport.requests import (
            Request,  # type: ignore[import-untyped]
        )

        credentials.refresh(Request())

    # def _get_id_token(self, audience):
    #     """Get an ID token if running as service account."""
    #     from google.auth.transport.requests import AuthorizedSession, Request
    #     from google.oauth2.id_token import fetch_id_token
    #     assert self._credentials is not None
    #     session = AuthorizedSession(self._credentials)
    #     request = Request(session)
    #     self._credentials.refresh(request)
    #     return fetch_id_token(request, audience)


    def _ensure_access_token(
        self,
        credentials: Optional[VERTEX_CREDENTIALS_TYPES],
        project_id: Optional[str],
        custom_llm_provider: Literal[
            "vertex_ai", "vertex_ai_beta", "gemini"
        ],  # if it's vertex_ai or gemini (google ai studio)
        api_base: Optional[str] = None,
    ) -> Tuple[str, str]:
        """
        Returns auth token and project id
        """
        # if custom_llm_provider == "gemini":
        #     return "", ""
        if self.access_token is not None:
            if project_id is not None:
                return self.access_token, project_id
            elif self.project_id is not None:
                return self.access_token, self.project_id

        if not self._credentials:
            self._credentials, cred_project_id = self.load_auth(
                credentials=credentials, project_id=project_id
            )
            if not self.project_id:
                self.project_id = project_id or cred_project_id
        else:
            if self._credentials.expired or not self._credentials.token:
                self.refresh_auth(self._credentials)

            if not self.project_id:
                self.project_id = self._credentials.quota_project_id

        if not self.project_id:
            raise ValueError("Could not resolve project_id")

        if not self._credentials or not self._credentials.token:
            raise RuntimeError("Could not resolve API token from the environment")

        if hasattr(self._credentials, "id_token") and self._credentials.id_token:
            return self._credentials.id_token, project_id or self.project_id
        return self._credentials.token, project_id or self.project_id

    def is_using_v1beta1_features(self, optional_params: dict) -> bool:
        """
        VertexAI only supports ContextCaching on v1beta1

        use this helper to decide if request should be sent to v1 or v1beta1

        Returns v1beta1 if context caching is enabled
        Returns v1 in all other cases
        """
        if "cached_content" in optional_params:
            return True
        if "CachedContent" in optional_params:
            return True
        return False

    def _check_custom_proxy(
        self,
        api_base: Optional[str],
        custom_llm_provider: str,
        gemini_api_key: Optional[str],
        endpoint: str,
        stream: Optional[bool],
        auth_header: Optional[str],
        url: str,
    ) -> Tuple[Optional[str], str]:
        """
        for cloudflare ai gateway - https://github.com/BerriAI/litellm/issues/4317

        ## Returns
        - (auth_header, url) - Tuple[Optional[str], str]
        """
        if api_base:
            if custom_llm_provider == "gemini":
                url = "{}:{}".format(api_base, endpoint)
                if gemini_api_key is None:
                    raise ValueError(
                        "Missing gemini_api_key, please set `GEMINI_API_KEY`"
                    )
                auth_header = (
                    gemini_api_key  # cloudflare expects api key as bearer token
                )
            else:
                url = "{}:{}".format(api_base, endpoint)

            if stream is True:
                url = url + "?alt=sse"
        return auth_header, url

    def _get_token_and_url(
        self,
        model: str,
        auth_header: Optional[str],
        gemini_api_key: Optional[str],
        vertex_project: Optional[str],
        vertex_location: Optional[str],
        vertex_credentials: Optional[VERTEX_CREDENTIALS_TYPES],
        stream: Optional[bool],
        custom_llm_provider: Literal["vertex_ai", "vertex_ai_beta", "gemini"],
        api_base: Optional[str],
        should_use_v1beta1_features: Optional[bool] = False,
        mode: all_gemini_url_modes = "chat",
    ) -> Tuple[Optional[str], str]:
        """
        Internal function. Returns the token and url for the call.

        Handles logic if it's google ai studio vs. vertex ai.

        Returns
            token, url
        """
        if custom_llm_provider == "gemini":
            url, endpoint = _get_gemini_url(
                mode=mode,
                model=model,
                stream=stream,
                gemini_api_key=gemini_api_key,
                api_base=api_base
            )
            # auth_header = None  # this field is not used for gemin
        else:
            vertex_location = self.get_vertex_region(vertex_region=vertex_location)

            ### SET RUNTIME ENDPOINT ###
            version: Literal["v1beta1", "v1"] = (
                "v1beta1" if should_use_v1beta1_features is True else "v1"
            )
            url, endpoint = _get_vertex_url(
                mode=mode,
                model=model,
                stream=stream,
                vertex_project=vertex_project,
                vertex_location=vertex_location,
                vertex_api_version=version,
            )
        return auth_header, url

        # return self._check_custom_proxy(
        #     api_base=api_base,
        #     auth_header=auth_header,
        #     custom_llm_provider=custom_llm_provider,
        #     gemini_api_key=gemini_api_key,
        #     endpoint=endpoint,
        #     stream=stream,
        #     url=url,
        # )

    async def _ensure_access_token_async(
        self,
        credentials: Optional[VERTEX_CREDENTIALS_TYPES],
        project_id: Optional[str],
        custom_llm_provider: Literal[
            "vertex_ai", "vertex_ai_beta", "gemini"
        ],  # if it's vertex_ai or gemini (google ai studio)
    ) -> Tuple[str, str]:
        """
        Async version of _ensure_access_token
        """
        if custom_llm_provider == "gemini":
            return "", ""
        if self.access_token is not None:
            if project_id is not None:
                return self.access_token, project_id
            elif self.project_id is not None:
                return self.access_token, self.project_id

        if not self._credentials:
            try:
                self._credentials, cred_project_id = await asyncify(self.load_auth)(
                    credentials=credentials, project_id=project_id
                )
            except Exception:
                verbose_logger.exception(
                    "Failed to load vertex credentials. Check to see if credentials containing partial/invalid information."
                )
                raise
            if not self.project_id:
                self.project_id = project_id or cred_project_id
        else:
            if self._credentials.expired or not self._credentials.token:
                await asyncify(self.refresh_auth)(self._credentials)

            if not self.project_id:
                self.project_id = self._credentials.quota_project_id

        if not self.project_id:
            raise ValueError("Could not resolve project_id")

        if not self._credentials or not self._credentials.token:
            raise RuntimeError("Could not resolve API token from the environment")

        return self._credentials.token, project_id or self.project_id

    def set_headers(
        self, auth_header: Optional[str], extra_headers: Optional[dict]
    ) -> dict:
        headers = {
            "Content-Type": "application/json",
        }
        if auth_header is not None:
            headers["Authorization"] = f"Bearer {auth_header}"
        if extra_headers is not None:
            headers.update(extra_headers)

        return headers

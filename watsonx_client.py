import time
import asyncio
import logging
from typing import Dict, Any, List, Optional, Tuple
import httpx
from app.config import settings

logger = logging.getLogger(__name__)

IAM_TOKEN_URL = "https://iam.cloud.ibm.com/identity/token"


class WatsonxError(Exception):
    def __init__(self, message: str, status_code: Optional[int] = None, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.status_code = status_code
        self.details = details or {}


class WatsonxClient:
    def __init__(self):
        self._cached_token: Optional[str] = None
        self._token_expiry: float = 0
        self._lock = asyncio.Lock()

    @property
    def api_key(self) -> Optional[str]:
        return settings.watsonx_api_key

    @property
    def project_id(self) -> Optional[str]:
        return settings.watsonx_project_id

    @property
    def base_url(self) -> str:
        url = settings.watsonx_url.rstrip("/")
        if not url.startswith("http"):
            url = f"https://{url}"
        return url

    @property
    def primary_model(self) -> str:
        return settings.watsonx_model_id

    @property
    def fallback_model(self) -> str:
        return settings.watsonx_fallback_model_id

    @property
    def api_version(self) -> str:
        return settings.watsonx_api_version

    def is_configured(self) -> bool:
        return bool(self.api_key and self.project_id)

    async def get_iam_token(self, api_key_override: Optional[str] = None, force_refresh: bool = False) -> str:
        """Fetch and cache an IBM Cloud IAM Bearer Token using the API Key."""
        api_key = api_key_override or self.api_key
        if not api_key:
            raise WatsonxError("IBM Cloud API Key (WATSONX_API_KEY) is not set.", status_code=401)

        # Return cached token if still valid (with 60s buffer)
        async with self._lock:
            if not force_refresh and not api_key_override and self._cached_token and time.time() < (self._token_expiry - 60):
                return self._cached_token

            headers = {
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json"
            }
            data = {
                "grant_type": "urn:ibm:params:oauth:grant-type:apikey",
                "apikey": api_key
            }

            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    response = await client.post(IAM_TOKEN_URL, headers=headers, data=data)
                    
                    if response.status_code != 200:
                        err_text = response.text
                        logger.error(f"IBM IAM Token generation failed with status {response.status_code}: {err_text}")
                        raise WatsonxError(
                            f"Failed to authenticate with IBM Cloud IAM (HTTP {response.status_code}). Check your WATSONX_API_KEY.",
                            status_code=response.status_code,
                            details={"response": err_text}
                        )

                    token_data = response.json()
                    access_token = token_data.get("access_token")
                    expires_in = token_data.get("expires_in", 3600)

                    if not access_token:
                        raise WatsonxError("IAM response missing access_token", status_code=500)

                    if not api_key_override:
                        self._cached_token = access_token
                        self._token_expiry = time.time() + float(expires_in)

                    return access_token

            except httpx.RequestError as e:
                logger.error(f"Network error connecting to IBM IAM: {e}")
                raise WatsonxError(f"Network error connecting to IBM Cloud IAM: {str(e)}", status_code=503)

    async def chat_completion(
        self,
        messages: List[Dict[str, str]],
        model_id: Optional[str] = None,
        project_id_override: Optional[str] = None,
        api_key_override: Optional[str] = None,
        max_tokens: int = 2048,
        temperature: float = 0.2,
        max_retries: int = 3
    ) -> Dict[str, Any]:
        """
        Send a chat completion request to IBM watsonx.ai REST API with:
        - Primary Granite code model (`ibm/granite-8b-code-instruct`)
        - Automatic fallback to `ibm/granite-3-8b-instruct`
        - Exponential backoff for rate limits and transient errors
        """
        if not self.is_configured() and not (api_key_override and project_id_override):
            raise WatsonxError(
                "watsonx.ai credentials missing. Please set WATSONX_API_KEY and WATSONX_PROJECT_ID.",
                status_code=400
            )

        project_id = project_id_override or self.project_id
        target_model = model_id or self.primary_model
        
        token = await self.get_iam_token(api_key_override=api_key_override)
        
        endpoint = f"{self.base_url}/ml/v1/text/chat?version={self.api_version}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

        async def _call_model(model_name: str) -> Tuple[str, str]:
            payload = {
                "model_id": model_name,
                "project_id": project_id,
                "messages": messages,
                "parameters": {
                    "temperature": temperature,
                    "max_new_tokens": max_tokens
                }
            }

            backoff = 1.0
            last_error = None

            for attempt in range(1, max_retries + 1):
                try:
                    async with httpx.AsyncClient(timeout=60.0) as client:
                        resp = await client.post(endpoint, headers=headers, json=payload)
                        
                        if resp.status_code == 200:
                            data = resp.json()
                            choices = data.get("choices", [])
                            if choices and "message" in choices[0]:
                                content = choices[0]["message"].get("content", "")
                                return content, model_name
                            
                            # Alternative response format sometimes returned by watsonx v1
                            results = data.get("results", [])
                            if results and "generated_text" in results[0]:
                                return results[0]["generated_text"], model_name
                            
                            return str(data), model_name

                        if resp.status_code == 401:
                            # Token might be expired, force refresh once
                            if attempt == 1:
                                refreshed_token = await self.get_iam_token(api_key_override=api_key_override, force_refresh=True)
                                headers["Authorization"] = f"Bearer {refreshed_token}"
                                continue
                            raise WatsonxError("Watsonx Authentication Failed (401). Verify credentials.", status_code=401)

                        if resp.status_code in (400, 404):
                            err_msg = resp.text
                            # If model is unavailable, raise to trigger model fallback
                            if "model" in err_msg.lower() or resp.status_code == 404:
                                raise WatsonxError(f"Model {model_name} unavailable: {err_msg}", status_code=resp.status_code)

                        if resp.status_code in (429, 500, 502, 503, 504):
                            last_error = f"HTTP {resp.status_code}: {resp.text}"
                            logger.warning(f"watsonx.ai rate limit / server error (attempt {attempt}/{max_retries}): {last_error}")
                            if attempt < max_retries:
                                await asyncio.sleep(backoff)
                                backoff *= 2.0
                                continue

                        raise WatsonxError(f"watsonx.ai error (HTTP {resp.status_code}): {resp.text}", status_code=resp.status_code)

                except httpx.RequestError as e:
                    last_error = f"Request error: {str(e)}"
                    logger.warning(f"watsonx.ai network attempt {attempt} failed: {e}")
                    if attempt < max_retries:
                        await asyncio.sleep(backoff)
                        backoff *= 2.0
                        continue
                    raise WatsonxError(f"watsonx.ai connection timeout/error: {str(e)}", status_code=504)

            raise WatsonxError(f"watsonx.ai request failed after {max_retries} attempts: {last_error}", status_code=500)

        # Execute call with primary model, fall back to secondary if primary fails with model not found
        try:
            content, used_model = await _call_model(target_model)
            return {"content": content, "model": used_model, "is_mock": False}
        except WatsonxError as primary_err:
            if target_model != self.fallback_model and (primary_err.status_code in (400, 404) or "model" in str(primary_err).lower()):
                logger.info(f"Primary model {target_model} failed. Falling back to {self.fallback_model}...")
                try:
                    content, used_model = await _call_model(self.fallback_model)
                    return {"content": content, "model": used_model, "is_mock": False, "fallback_triggered": True}
                except Exception as fallback_err:
                    raise WatsonxError(f"Both primary ({target_model}) and fallback ({self.fallback_model}) models failed: {fallback_err}")
            raise

    async def test_connection(
        self,
        api_key: Optional[str] = None,
        project_id: Optional[str] = None,
        url: Optional[str] = None,
        model_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Test watsonx credentials by requesting an IAM token and sending a lightweight prompt."""
        key = api_key or self.api_key
        pid = project_id or self.project_id
        
        if not key or not pid:
            return {
                "success": False,
                "configured": False,
                "message": "Missing credentials. WATSONX_API_KEY and WATSONX_PROJECT_ID are required."
            }

        try:
            start_time = time.time()
            token = await self.get_iam_token(api_key_override=key, force_refresh=True)
            
            # Simple ping prompt to Granite model
            messages = [
                {"role": "system", "content": "You are a code intelligence assistant. Respond in one word."},
                {"role": "user", "content": "Respond with: READY"}
            ]
            
            result = await self.chat_completion(
                messages=messages,
                model_id=model_id or self.primary_model,
                project_id_override=pid,
                api_key_override=key,
                max_tokens=20,
                temperature=0.0
            )
            latency_ms = int((time.time() - start_time) * 1000)
            
            return {
                "success": True,
                "configured": True,
                "connected": True,
                "model_used": result["model"],
                "response_text": result["content"].strip(),
                "latency_ms": latency_ms,
                "message": f"Successfully connected to IBM watsonx.ai ({result['model']}) in {latency_ms}ms"
            }
        except Exception as e:
            return {
                "success": False,
                "configured": True,
                "connected": False,
                "message": f"Connection failed: {str(e)}"
            }


watsonx_client = WatsonxClient()

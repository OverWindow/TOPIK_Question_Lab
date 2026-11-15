from types import SimpleNamespace

import httpx
import pytest

from topik_question_lab.common.media.media_provider import GoogleMediaClient, MediaError, MediaSettings


def mock_models(respond, *, location="global", project="synthetic-project"):
    return GoogleMediaClient(MediaSettings.from_env(image_provider="google", project=project, image_location=location),
        credentials=SimpleNamespace(valid=True, token="synthetic-google-token"),
        client=httpx.Client(transport=httpx.MockTransport(respond)))


def model(name):
    return {"name": f"publishers/google/models/{name}"}


@pytest.mark.parametrize("location,host", [("global", "aiplatform.googleapis.com"),
                                          ("us-central1", "us-central1-aiplatform.googleapis.com")])
def test_google_catalog_paginates_filters_and_keeps_new_image_models(location, host):
    requests = []

    def respond(request):
        requests.append(request)
        if "pageToken" not in request.url.params:
            return httpx.Response(200, json={"publisherModels": [
                model("gemini-3.1-flash-image"), model("gemini-3.1-flash-image@001"),
                model("gemini-3-pro"), model("imagen-4.0-generate-001"), model("veo-3.0-generate-001"),
                model("gemini-embedding-image"), model("gemini-2.5-flash-image"),
                model("gemini-2.0-flash-preview-image-generation"), model("gemini-3-pro-image/invalid"),
                model("gemini-3-pro-image invalid"), {"name": "publishers/other/models/gemini-3-pro-image"},
                {"name": 123}, {}, None,
            ], "nextPageToken": "next-page"})
        if request.url.params["pageToken"] == "next-page":
            return httpx.Response(200, json={"publisherModels": [
                model("gemini-99-flash-image"), model("gemini-nano-banana-99"),
                model("gemini-3-pro-image-preview"), model("gemini-3.1-flash-image"),
            ], "nextPageToken": "empty-page"})
        return httpx.Response(200, json={})

    client = mock_models(respond, location=location)
    try:
        assert client.list_models() == ["gemini-3-pro-image-preview", "gemini-3.1-flash-image",
                                        "gemini-99-flash-image", "gemini-nano-banana-99"]
    finally:
        client.close()
    assert len(requests) == 3
    assert all(request.method == "GET" and not request.content for request in requests)
    assert all(request.url.host == host and request.url.path == "/v1beta1/publishers/google/models" for request in requests)
    assert all(request.url.params["pageSize"] == "100" for request in requests)
    assert requests[1].url.params["pageToken"] == "next-page"
    assert requests[2].url.params["pageToken"] == "empty-page"
    assert all(request.headers["Authorization"] == "Bearer synthetic-google-token" for request in requests)
    assert all(request.headers["x-goog-user-project"] == "synthetic-project" for request in requests)


@pytest.mark.parametrize("status", [401, 403, 429, 503])
def test_google_catalog_http_errors_hide_credentials_and_response_body(status):
    client = mock_models(lambda request: httpx.Response(status, json={"error": "synthetic-google-token"}))
    try:
        with pytest.raises(MediaError, match=f"HTTP {status}") as caught:
            client.list_models()
        assert "synthetic-google-token" not in str(caught.value)
        assert "synthetic-project" not in str(caught.value)
    finally:
        client.close()


def test_google_catalog_transport_errors_are_safe():
    def respond(request):
        raise httpx.ConnectError("synthetic-google-token", request=request)

    client = mock_models(respond)
    try:
        with pytest.raises(MediaError, match="목록 조회") as caught:
            client.list_models()
        assert "synthetic-google-token" not in str(caught.value)
    finally:
        client.close()


@pytest.mark.parametrize("payload", [None, [], {"publisherModels": {}}, {"publisherModels": None},
                                     {"publisherModels": [], "nextPageToken": None},
                                     {"publisherModels": [], "nextPageToken": ["bad"]}])
def test_google_catalog_rejects_malformed_responses(payload):
    import json

    client = mock_models(lambda request: httpx.Response(200, content=json.dumps(payload)))
    try:
        with pytest.raises(MediaError, match="응답"):
            client.list_models()
    finally:
        client.close()


def test_google_catalog_rejects_invalid_json():
    client = mock_models(lambda request: httpx.Response(200, text="synthetic-google-token"))
    try:
        with pytest.raises(MediaError, match="응답") as caught:
            client.list_models()
        assert "synthetic-google-token" not in str(caught.value)
    finally:
        client.close()


@pytest.mark.parametrize("mode", ["repeated-token", "later-error"])
def test_google_catalog_does_not_return_partial_results(mode):
    requests = []

    def respond(request):
        requests.append(request)
        if mode == "later-error" and len(requests) > 1:
            return httpx.Response(403, text="synthetic-google-token")
        return httpx.Response(200, json={"publisherModels": [model("gemini-3-pro-image")], "nextPageToken": "repeat"})

    client = mock_models(respond)
    try:
        with pytest.raises(MediaError):
            client.list_models()
    finally:
        client.close()
    assert len(requests) == 2


def test_google_catalog_requires_project_and_authentication_before_request(monkeypatch):
    client = mock_models(lambda request: pytest.fail("must not request without configured authentication"), project="")
    try:
        with pytest.raises(MediaError, match="GOOGLE_CLOUD_PROJECT_ID"):
            client.list_models()
        client.settings = MediaSettings.from_env(project="synthetic-project")

        def fail_auth():
            raise MediaError("Google 인증에 실패했습니다.")

        monkeypatch.setattr(client, "authenticate", fail_auth)
        with pytest.raises(MediaError, match="Google 인증"):
            client.list_models()
    finally:
        client.close()

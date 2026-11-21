import base64
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import noxgate_gpt_image as image


PNG = b"\x89PNG\r\n\x1a\nfixture"


class NoxGateGptImageTests(unittest.TestCase):
    def provider_config(self, root: Path, base_url=image.EXPECTED_BASE_URL, token="fixture-token") -> Path:
        path = root / "config.toml"
        path.write_text(
            'model_provider = "noxgate"\n[model_providers.noxgate]\n'
            f'base_url = "{base_url}"\nexperimental_bearer_token = "{token}"\n',
            encoding="utf-8",
        )
        return path

    def test_provider_requires_noxgate_base_url_and_reads_bearer_token(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self.provider_config(Path(directory))
            self.assertEqual(image.load_provider(config), (image.EXPECTED_BASE_URL, "fixture-token"))
            other = self.provider_config(Path(directory), "https://api.noxgate.net/v1")
            with self.assertRaisesRegex(image.ImageRequestError, "Base URL"):
                image.load_provider(other)

    def test_listing_filters_to_authorized_gpt_image_models_and_keeps_latest_variants(self):
        authorized = {"data": [
            {"id": "gpt-image-2"},
            {"id": "gpt-image-2.5-flare"},
            {"id": "gpt-image-2.6-flare"},
            {"id": "gpt-image-2.6-sunburst"},
            {"id": "seedream-5"},
        ]}
        catalog = {"data": [
            {"model_id": "gpt-image-2", "capability_type": "image_generation"},
            {"model_id": "gpt-image-2.5-flare", "capability_type": "image_generation"},
            {"model_id": "gpt-image-2.6-flare", "capability_type": "image_generation",
             "price_config": {"rules": [{"meter": "image_count", "price": 20000}]}},
            {"model_id": "gpt-image-2.6-sunburst", "capability_type": "image_generation"},
        ]}
        with patch.object(image, "load_provider", return_value=(image.EXPECTED_BASE_URL, "fixture-token")), \
             patch.object(image, "request_json", side_effect=[authorized, catalog, {"data": {"payment_currency": "usd"}}]):
            result = image.listing("generate", count=2)
        self.assertEqual([model["id"] for model in result["models"]], [
            "gpt-image-2", "gpt-image-2.5-flare", "gpt-image-2.6-flare", "gpt-image-2.6-sunburst"
        ])
        self.assertEqual(result["latest_model_ids"], ["gpt-image-2.6-flare", "gpt-image-2.6-sunburst"])
        self.assertEqual(result["models"][2]["estimated_base_cost"], "0.04")

    def test_edit_models_are_selected_from_catalog_capabilities(self):
        authorized = {"data": [
            {"id": "gpt-image-2.6-flare"},
            {"id": "gpt-image-2.7-flare"},
            {"id": "gpt-image-2.7-chat-only"},
        ]}
        catalog = {"data": [
            {"model_id": "gpt-image-2.6-flare", "capability_type": "image_generation",
             "supported_client_protocols": ["openai_images"]},
            {"model_id": "gpt-image-2.7-flare", "capability_type": "image_generation",
             "supported_client_protocols": ["openai_images"]},
            {"model_id": "gpt-image-2.7-chat-only", "capability_type": "image_generation",
             "supported_client_protocols": ["openai_chat_completions"]},
        ]}
        with patch.object(image, "request_json", side_effect=[authorized, catalog]):
            models, catalog_available = image.authorized_models(
                image.EXPECTED_BASE_URL, "fixture-token", "edit"
            )
        self.assertTrue(catalog_available)
        self.assertEqual([model["id"] for model in models], [
            "gpt-image-2.6-flare", "gpt-image-2.7-flare"
        ])
        self.assertTrue(all(model["operation_support"] == "confirmed" for model in models))

    def test_generation_requires_explicit_model_or_default_flag(self):
        args = image.build_parser().parse_args([
            "generate", "--prompt", "a blue cup", "--output", "/tmp/fixture.png"
        ])
        with patch.object(image, "load_provider", return_value=(image.EXPECTED_BASE_URL, "fixture-token")), \
             patch.object(image, "authorized_models", return_value=([{"id": "gpt-image-2.6-flare"}], True)), \
             patch.object(image, "listing", return_value={"models": [{"id": "gpt-image-2.6-flare"}]}) as listing, \
             patch("sys.stdout", new_callable=io.StringIO) as stdout, \
             patch("sys.stderr", new_callable=io.StringIO) as stderr:
            self.assertEqual(image.submit(args), 2)
        listing.assert_called_once_with("generate", 1)
        self.assertIn("gpt-image-2.6-flare", stdout.getvalue())
        self.assertIn("选择模型", stderr.getvalue())

    def test_generation_sends_the_selected_model_without_using_a_default(self):
        args = image.build_parser().parse_args([
            "generate", "--prompt", "a blue cup", "--model", "gpt-image-2.6-sunburst",
            "--output", "/tmp/fixture.png"
        ])
        with patch.object(image, "load_provider", return_value=(image.EXPECTED_BASE_URL, "fixture-token")), \
             patch.object(image, "authorized_models", return_value=([{"id": "gpt-image-2.6-sunburst"}], True)), \
             patch.object(image, "request_json", return_value={"data": [{"b64_json": base64.b64encode(PNG).decode()}]}) as request:
            self.assertEqual(image.submit(args), 0)
        payload = json.loads(request.call_args.kwargs["data"])
        self.assertEqual(payload["model"], "gpt-image-2.6-sunburst")
        self.assertEqual(payload["n"], 1)
        Path("/tmp/fixture.png").unlink(missing_ok=True)

    def test_default_flag_uses_default_model_only_when_requested(self):
        args = image.build_parser().parse_args([
            "generate", "--prompt", "a blue cup", "--use-default", "--output", "/tmp/fixture.png"
        ])
        with patch.object(image, "load_provider", return_value=(image.EXPECTED_BASE_URL, "fixture-token")), \
             patch.object(image, "authorized_models", return_value=([{"id": image.DEFAULT_MODEL}], True)), \
             patch.object(image, "request_json", return_value={"data": [{"b64_json": base64.b64encode(PNG).decode()}]}) as request:
            self.assertEqual(image.submit(args), 0)
        self.assertEqual(json.loads(request.call_args.kwargs["data"])["model"], image.DEFAULT_MODEL)
        Path("/tmp/fixture.png").unlink(missing_ok=True)

    def test_image_download_requires_https_before_making_a_request(self):
        with patch("urllib.request.urlopen") as urlopen:
            with self.assertRaisesRegex(image.ImageRequestError, "HTTPS"):
                image.image_bytes({"url": "http://noxgate.net/image.png"}, image.EXPECTED_BASE_URL,
                                  "fixture-token")
        urlopen.assert_not_called()

    def test_image_download_only_sends_token_to_the_same_https_origin(self):
        with patch("urllib.request.build_opener") as build_opener:
            build_opener.return_value.open.return_value = io.BytesIO(PNG)
            image.image_bytes({"url": "https://noxgate.net/image.png"}, image.EXPECTED_BASE_URL,
                              "fixture-token")
        request = build_opener.return_value.open.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer fixture-token")

        with patch("urllib.request.build_opener") as build_opener:
            build_opener.return_value.open.return_value = io.BytesIO(PNG)
            image.image_bytes({"url": "https://cdn.example/image.png"}, image.EXPECTED_BASE_URL,
                              "fixture-token")
        request = build_opener.return_value.open.call_args.args[0]
        self.assertIsNone(request.get_header("Authorization"))

    def test_image_redirect_strips_token_for_a_different_origin(self):
        handler = image.ImageRedirectHandler(image.url_origin(image.EXPECTED_BASE_URL))
        request = image.urllib.request.Request("https://noxgate.net/image.png",
                                               headers={"Authorization": "Bearer fixture-token"})
        redirected = handler.redirect_request(request, None, 302, "Found", {},
                                              "https://cdn.example/image.png")
        self.assertIsNotNone(redirected)
        self.assertIsNone(redirected.get_header("Authorization"))

    def test_image_redirect_rejects_http(self):
        handler = image.ImageRedirectHandler(image.url_origin(image.EXPECTED_BASE_URL))
        request = image.urllib.request.Request("https://noxgate.net/image.png",
                                               headers={"Authorization": "Bearer fixture-token"})
        redirected = handler.redirect_request(request, None, 302, "Found", {},
                                              "http://noxgate.net/image.png")
        self.assertIsNone(redirected)

    def test_edit_sends_requested_image_count(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reference = root / "reference.png"
            reference.write_bytes(PNG)
            output = root / "edited.png"
            args = image.build_parser().parse_args([
                "edit", "--prompt", "a blue cup", "--model", "gpt-image-2.6-flare",
                "--count", "3", "--image", str(reference), "--output", str(output)
            ])
            with patch.object(image, "load_provider", return_value=(image.EXPECTED_BASE_URL, "fixture-token")), \
                 patch.object(image, "authorized_models", return_value=([{"id": "gpt-image-2.6-flare"}], True)), \
                 patch.object(image, "request_json", return_value={
                     "data": [{"b64_json": base64.b64encode(PNG).decode()}]
                 }) as request:
                self.assertEqual(image.submit(args), 0)
            body = request.call_args.kwargs["data"]
            self.assertIn(b'Content-Disposition: form-data; name="n"\r\n\r\n3', body)
            self.assertTrue(output.is_file())


if __name__ == "__main__":
    unittest.main()

import importlib.util
import os
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]


def load_config_module():
    # Load utils/config.py on its own: importing the utils package also imports
    # the OpenAI client, which these tests do not need.
    spec = importlib.util.spec_from_file_location("evolve_config_under_test", REPO_ROOT / "utils" / "config.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ArobiExperimentConfigTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config_module()

    def test_arobi_experiment_routes_through_the_q_gateway(self):
        env = {"IMMACULATE_Q_GATEWAY_BASE_URL": "http://127.0.0.1:8897/v1", "IMMACULATE_Q_API_KEY": "q-key-for-test"}
        with mock.patch.dict(os.environ, env):
            resolved = self.config.load_config(experiment_name="arobi")
        self.assertEqual(resolved["experiment_name"], "arobi")
        self.assertEqual(resolved["api"]["base_url"], "http://127.0.0.1:8897/v1")
        self.assertEqual(resolved["api"]["api_key"], "q-key-for-test")
        self.assertEqual(resolved["api"]["model"], "Q")
        self.assertFalse(resolved["logging"]["wandb"]["enabled"])
        self.config.validate_api_config(resolved)

    def test_unset_gateway_variables_are_refused(self):
        with mock.patch.dict(os.environ, {"IMMACULATE_Q_GATEWAY_BASE_URL": "", "IMMACULATE_Q_API_KEY": ""}):
            resolved = self.config.load_config(experiment_name="arobi")
        with self.assertRaises(self.config.ConfigError) as caught:
            self.config.validate_api_config(resolved)
        self.assertIn("api.base_url is empty", str(caught.exception))
        self.assertIn("api.api_key is empty", str(caught.exception))

    def test_root_config_placeholders_are_refused_with_a_clear_error(self):
        resolved = self.config.load_config()
        self.assertEqual(resolved["api"]["base_url"], "your_base_url")
        with self.assertRaises(self.config.ConfigError) as caught:
            self.config.validate_api_config(resolved)
        message = str(caught.exception)
        for key in ("base_url", "api_key", "model"):
            self.assertIn(f"api.{key} is still the upstream placeholder", message)
        self.assertIn("--experiment arobi", message)

    def test_llm_client_factory_validates_before_building_a_client(self):
        source = (REPO_ROOT / "utils" / "llm.py").read_text(encoding="utf-8")
        factory = source[source.index("def create_llm_client"):]
        self.assertLess(factory.index("validate_api_config(config)"), factory.index("LLMClient("))


if __name__ == "__main__":
    unittest.main()

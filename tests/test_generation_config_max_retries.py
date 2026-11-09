import tempfile
import unittest
from unittest.mock import patch

from src.core.config import DEFAULT_FLOW_MAX_RETRIES, Config, config
from src.core.database import Database
from src.core.models import GenerationConfig


class GenerationAttemptsDefaultTests(unittest.TestCase):
    def test_model_and_runtime_missing_or_invalid_default_to_twenty(self):
        self.assertEqual(DEFAULT_FLOW_MAX_RETRIES, 20)
        self.assertEqual(GenerationConfig().max_retries, 20)
        with patch.object(Config, "_load_config", return_value={}):
            settings = Config()
        self.assertEqual(settings.flow_max_retries, 20)
        for invalid in (None, "invalid"):
            settings._config = {"flow": {"max_retries": invalid}}
            self.assertEqual(settings.flow_max_retries, 20)
            settings.set_flow_max_retries(invalid)
            self.assertEqual(settings.flow_max_retries, 20)
        settings.set_flow_max_retries(3)
        self.assertEqual(settings.flow_max_retries, 3)
        settings.set_flow_max_retries(0)
        self.assertEqual(settings.flow_max_retries, 1)


class GenerationConfigMaxRetriesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(db_path=f"{self._temp_dir.name}/flow.db")
        self._original_image_timeout = config.image_timeout
        self._original_video_timeout = config.video_timeout
        self._original_max_retries = config.flow_max_retries
        await self.db.init_db()

    async def asyncTearDown(self):
        config.set_image_timeout(self._original_image_timeout)
        config.set_video_timeout(self._original_video_timeout)
        config.set_flow_max_retries(self._original_max_retries)
        self._temp_dir.cleanup()

    async def test_init_config_from_toml_persists_flow_max_retries(self):
        await self.db.init_config_from_toml(
            {
                "generation": {
                    "image_timeout": 321,
                    "video_timeout": 654,
                },
                "flow": {
                    "max_retries": 7,
                },
            },
            is_first_startup=True,
        )

        generation_config = await self.db.get_generation_config()

        self.assertIsNotNone(generation_config)
        self.assertEqual(generation_config.image_timeout, 321)
        self.assertEqual(generation_config.video_timeout, 654)
        self.assertEqual(generation_config.max_retries, 7)

    async def test_empty_initial_configuration_defaults_to_twenty_and_reloads(self):
        await self.db.init_config_from_toml({}, is_first_startup=True)
        self.assertEqual((await self.db.get_generation_config()).max_retries, 20)
        await self.db.reload_config_to_memory()
        self.assertEqual(config.flow_max_retries, 20)

    async def test_invalid_initial_configuration_uses_twenty(self):
        await self.db.init_config_from_toml(
            {"flow": {"max_retries": "invalid"}}, is_first_startup=True
        )
        self.assertEqual((await self.db.get_generation_config()).max_retries, 20)

    async def test_fresh_sql_default_and_legacy_missing_column_use_twenty(self):
        async with self.db._connect(write=True) as connection:
            await connection.execute("INSERT INTO generation_config (id) VALUES (1)")
            await connection.commit()
        self.assertEqual((await self.db.get_generation_config()).max_retries, 20)
        async with self.db._connect(write=True) as connection:
            await connection.execute("ALTER TABLE generation_config DROP COLUMN max_retries")
            await connection.commit()
        await self.db.check_and_migrate_db({"flow": {"max_retries": 3}})
        self.assertEqual((await self.db.get_generation_config()).max_retries, 20)

    async def test_existing_explicit_three_survives_new_defaults_and_reload(self):
        await self.db.init_config_from_toml({"flow": {"max_retries": 3}}, is_first_startup=True)
        await self.db.check_and_migrate_db({"flow": {"max_retries": 20}})
        await self.db.reload_config_to_memory()
        self.assertEqual((await self.db.get_generation_config()).max_retries, 3)
        self.assertEqual(config.flow_max_retries, 3)
        await self.db.update_generation_config(image_timeout=450)
        self.assertEqual((await self.db.get_generation_config()).max_retries, 3)

    async def test_reload_config_to_memory_syncs_max_retries(self):
        await self.db.init_config_from_toml(
            {
                "generation": {
                    "image_timeout": 300,
                    "video_timeout": 1500,
                },
                "flow": {
                    "max_retries": 3,
                },
            },
            is_first_startup=True,
        )

        await self.db.update_generation_config(max_retries=9)
        await self.db.reload_config_to_memory()

        self.assertEqual(config.flow_max_retries, 9)


if __name__ == "__main__":
    unittest.main()

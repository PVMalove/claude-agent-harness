"""Экраны Textual для консоли harness. Каждый модуль здесь импортирует `textual` при загрузке —
только harness.console.app импортирует этот пакет и только из уже перезапущенного подпроцесса
`uv run --with textual==<pin>` (см. harness.console.launcher.run_console)."""

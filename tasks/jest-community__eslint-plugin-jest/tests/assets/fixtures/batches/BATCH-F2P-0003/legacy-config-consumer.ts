import type { Linter } from 'eslint';
import plugin = require('@plugin');

const legacyConfig: Linter.LegacyConfig = plugin.configs.recommended;

void legacyConfig;

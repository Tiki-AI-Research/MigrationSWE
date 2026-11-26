#!/usr/bin/env node
import { main } from './shared/integration/run-integration.mjs';

const code = await main({ side: 'target', argv: process.argv.slice(2) });
process.exitCode = code;

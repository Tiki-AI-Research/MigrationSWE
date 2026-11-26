#!/usr/bin/env node
'use strict';

const path = require('path');
const shared = require(path.resolve(__dirname, '../../../shared/batches/BATCH-F2P-0002/cases.cjs'));

shared.runAll({ side: 'target', argv: process.argv.slice(2) });

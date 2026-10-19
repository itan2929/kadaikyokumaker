#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib';
import { KadaikyokumakerStack } from '../lib/kadaikyokumaker-stack';

const app = new cdk.App();
new KadaikyokumakerStack(app, 'KadaikyokumakerStack', {
  env: { account: process.env.CDK_DEFAULT_ACCOUNT, region: 'us-east-1' },
});

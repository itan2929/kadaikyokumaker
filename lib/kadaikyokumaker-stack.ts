import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as lambdaEventSources from 'aws-cdk-lib/aws-lambda-event-sources';
import * as sqs from 'aws-cdk-lib/aws-sqs';

export class KadaikyokumakerStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    const interactionQueue = new sqs.Queue(this, "FollowupQueue", {
      deliveryDelay: cdk.Duration.seconds(2),
      visibilityTimeout: cdk.Duration.seconds(180),
      retentionPeriod: cdk.Duration.days(1),
      encryption: sqs.QueueEncryption.SQS_MANAGED,
      enforceSSL: true,
    });

    const image = lambda.DockerImageCode.fromImageAsset("./src");
    const applicationId = "1344306931598360627";
    const publicKey = "05681b371f7c4b45476777eb4a8cb10e86f7ebb32a9b1e1fceace4223ef31a78";

    const dockerFunction = new lambda.DockerImageFunction(this, "DockerFunction", {
      code: image,
      memorySize: 1024,
      timeout: cdk.Duration.seconds(10),
      architecture: lambda.Architecture.X86_64,
      environment: {
        DISCORD_APPLICATION_ID: applicationId,
        DISCORD_PUBLIC_KEY: publicKey,
        FOLLOWUP_QUEUE_URL: interactionQueue.queueUrl,
      },
    });
    interactionQueue.grantSendMessages(dockerFunction);

    const followupFunction = new lambda.DockerImageFunction(this, "FollowupFunction", {
      code: image,
      memorySize: 1024,
      timeout: cdk.Duration.seconds(60),
      architecture: lambda.Architecture.X86_64,
      environment: {
        DISCORD_APPLICATION_ID: applicationId,
      },
    });
    const apiKeyParameterArn = this.formatArn({
      service: "ssm",
      resource: "parameter",
      resourceName: "kadaikyokumaker/chunisupport-api-key",
    });
    followupFunction.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["ssm:GetParameter"],
        resources: [apiKeyParameterArn],
      }),
    );
    followupFunction.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["kms:Decrypt"],
        resources: [
          this.formatArn({
            service: "kms",
            resource: "key",
            resourceName: "7a877c6b-3357-46de-8b12-11f9911e76c2",
          }),
        ],
        conditions: {
          StringEquals: {
            "kms:EncryptionContext:PARAMETER_ARN": apiKeyParameterArn,
          },
        },
      }),
    );
    followupFunction.addEventSource(
      new lambdaEventSources.SqsEventSource(interactionQueue, { batchSize: 1 }),
    );

    const functionUrl = dockerFunction.addFunctionUrl({
      authType: lambda.FunctionUrlAuthType.NONE,
      cors: {
        allowedOrigins: ["*"],
        allowedMethods: [lambda.HttpMethod.ALL],
        allowedHeaders: ["*"],
      },
    });

    new cdk.CfnOutput(this, "FunctionUrl", {
      value: functionUrl.url,
    });
  }
}

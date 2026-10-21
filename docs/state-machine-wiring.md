# Wiring ResolveGatewayAddress into `deploy_monitoring_room`

RFC assumption **A-02** left the state's name and insertion point to the
pipeline owner. This is the concrete change, written against the state
machine as it stands, for that owner to review.

The state machine is Terraform, not SAM:

- `mt-core/modules/stepfunctions/innomeshui_deploymonitoringroom.tf`
- resource `aws_sfn_state_machine.innomeshui_deploymonitoringroom`
- deployed as `innomesh-innomeshui-deployMonitoringRoom-v1`
- ARN read at runtime from SSM
  `/cap-innomesh-v1/properties/{env}/deploy_monitoring_room_state_machine_arn`

## Where it goes

The asset items are written by the **`Save asset metadata`** state
(`innomesh-cloudcontrol-pushAssetMetadata-v1`), reached from the `Task code`
choice when `$manifest.tasks[$task_index].code` is
`generic-save-asset-metadata`. That state currently goes straight to
`Prepare User Feedback - Post-subtask`.

`ResolveGatewayAddress` is inserted between the two. Because both the create
and the update path walk the same manifest loop, and mass create replays the
same loop per room, the single insertion covers all three.

```
Save asset metadata  →  Resolve gateway addresses  →  Prepare User Feedback - Post-subtask
```

## The change

Point `Save asset metadata` at the new state:

```hcl
"Save asset metadata" = {
  # ...unchanged...
  Next = "Resolve gateway addresses"   # was "Prepare User Feedback - Post-subtask"
}
```

and add the state:

```hcl
"Resolve gateway addresses" = {
  Arguments = {
    FunctionName = "${var.lambdas["innomesh-cloudcontrol-ResolveGatewayAddress-v1"]}:$LATEST"
    Payload = {
      code           = "{% $manifest.tasks[$task_index].subtasks[$subtask_index].code %}"
      correlation_id = "{% $uuid() %}"
      metadata       = "{% $metadata %}"
      namespace      = "{% $namespace %}"
      payload        = "{% $subtask_payload %}"
      retry          = "{% $retry %}"
      room           = "{% $room %}"
      tenant         = "{% $tenant %}"
    }
  }
  Assign = {
    error_flag      = "{% $states.result.Payload.error %}"
    message         = "{% $states.result.Payload.message %}"
    subtask_payload = "{% $states.result.Payload.payload %}"
  }
  Next     = "Prepare User Feedback - Post-subtask"
  Output   = "{% $states.result.Payload %}"
  Resource = "arn:aws:states:::lambda:invoke"
  Retry = [{
    BackoffRate     = 2
    ErrorEquals     = ["Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.SdkClientException", "Lambda.TooManyRequestsException"]
    IntervalSeconds = 1
    JitterStrategy  = "FULL"
    MaxAttempts     = 3
  }]
  Type = "Task"
}
```

The Payload block is copied from `Save asset metadata` deliberately: the two
states read the same `$subtask_payload`, which for the
`generic-save-asset-metadata` task is `$request_payload` and therefore
carries the `monitoring` and `asset_metadata` presigned URLs.

The `Retry` block only retries AWS-side Lambda faults. A resolution failure
is returned as `error: true` in the result, which the existing
`Encountered Error?` choice routes to `Log deployment fail` and then `Fail`.
That is the RFC's "a failure in this state fails the deployment"; there is
no partial-success path.

## IAM

The function runs under `the SAM-managed role in template.yaml.template`. That role
needs, for the deployed namespaces:

- `dynamodb:BatchGetItem` and `dynamodb:UpdateItem` on
  `arn:aws:dynamodb:*:*:table/assets.*`
- `ssm:GetParameter` on
  `arn:aws:ssm:*:*:parameter/cap-innomesh-v1/*/aws/kms/*/public-key`, and
  `kms:Decrypt` for the SSM SecureString key — only needed by rooms that
  author a manual gateway password
- VPC ENI permissions, already held by the shared role

The presigned artefact URLs need no IAM: they are fetched over plain HTTPS.

## Verifying the insertion

After deploying to Test, run a room deploy and look for the summary line:

```
INFO resolve_gateway room_id="b1g12" table="assets.curtin.uat.innomesh.com.au" \
  resolved_gateways=2 manual=0 cleared=1 dangling=0 written=3 assets_missing=0
```

`assets_missing` in the warning line greater than zero means the state ran before the asset
items existed, which means it was inserted too early.

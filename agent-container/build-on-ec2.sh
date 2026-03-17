#!/bin/bash
set -ex
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
REGION=$(aws configure get region || echo "us-east-1")
ECR_URI="${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com/openclaw-agent"
S3_BUCKET="openclaw-tenants-${ACCOUNT}-${REGION}"
LOG="/tmp/build.log"

exec > >(tee "$LOG") 2>&1

cd /tmp && rm -rf docker-build && mkdir docker-build && cd docker-build
aws s3 cp s3://${S3_BUCKET}/_build/agent-build.tar.gz . --region ${REGION}
tar xzf agent-build.tar.gz

aws ecr get-login-password --region ${REGION} | docker login --username AWS --password-stdin ${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com

docker build -f agent-container/Dockerfile -t ${ECR_URI}:latest .
docker push ${ECR_URI}:latest

echo "BUILD_AND_PUSH_COMPLETE"
aws s3 cp "$LOG" s3://${S3_BUCKET}/_build/build.log --region ${REGION}

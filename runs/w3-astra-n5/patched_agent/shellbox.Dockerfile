# Sandbox image for the shell_gpt proof target.
#
# The agent under test is TheR1D/shell_gpt's DEFAULT_ROLE, whose one tool executes arbitrary
# shell commands. Upstream runs them on the user's host; upshift runs them here instead, so an
# eval suite is safe to run and, more importantly, deterministic: a fixed base image is a fixed
# set of tool versions.
#
# Deliberately minimal. python:3.12-slim ships bash, coreutils, grep, sed, gawk/mawk and
# python3; it does NOT ship jq. That absence is honest, not an oversight — shell_gpt's own
# README documents the model working around a missing jq, and the parse_json_field case
# exercises exactly that.
#
# Nothing is installed on top. Containers are started with --network none, so a model that
# tries to apt-get or pip install a missing tool fails the way it would on an air-gapped box.
#
#   docker build -t upshift-shellbox:latest -f agents/shell_gpt/shellbox.Dockerfile agents/shell_gpt/
FROM python:3.12-slim

ENV TZ=UTC \
    LC_ALL=C \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /work

CMD ["bash"]

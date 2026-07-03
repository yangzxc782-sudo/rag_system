FROM opensearchproject/opensearch:3.6.0

ARG ANALYSIS_IK_URL=https://release.infinilabs.com/analysis-ik/stable/opensearch-analysis-ik-3.6.0.zip

RUN /usr/share/opensearch/bin/opensearch-plugin install --batch "${ANALYSIS_IK_URL}"

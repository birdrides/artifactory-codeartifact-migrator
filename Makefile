VERSION := $(shell jq -r '.version' version.json)

build-push:
	export AWS_PROFILE=bird-svc && \
	export AWS_REGION=us-west-2 && \
	docker build --platform linux/amd64 -t acm-migration:latest . && \
	echo "VERSION: $(VERSION)" && \
	docker tag acm-migration:latest 168995956934.dkr.ecr.us-west-2.amazonaws.com/acm-migration:$(VERSION) && \
	docker push 168995956934.dkr.ecr.us-west-2.amazonaws.com/acm-migration:$(VERSION)

update-image-tag:
	sed -i '' 's/image_tag[[:space:]]*=[[:space:]]*"[^"]*"/image_tag = "$(VERSION)"/' terraform/locals.tf
	@echo "Updated image_tag to $(VERSION) in terraform/locals.tf"

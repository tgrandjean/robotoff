import abc
import datetime
import pathlib
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional

from robotoff.insights._enum import InsightType
from robotoff.insights.normalize import normalize_emb_code
from robotoff.models import ProductInsight, db
from robotoff.off import (
    OFFAuthentication,
    add_brand,
    add_category,
    add_label_tag,
    add_packaging,
    add_store,
    save_ingredients,
    select_rotate_image,
    update_emb_codes,
    update_expiration_date,
    update_quantity,
)
from robotoff.products import get_image_id, get_product
from robotoff.utils import get_logger
from robotoff.utils.types import JSONType

logger = get_logger(__name__)


@dataclass
class AnnotationResult:
    status: str
    description: Optional[str] = None


class AnnotationStatus(Enum):
    saved = 1
    updated = 2
    error_missing_product = 3
    error_updated_product = 4
    error_already_annotated = 5
    error_unknown_insight = 6
    error_latent_insight = 7
    error_missing_data = 8


SAVED_ANNOTATION_RESULT = AnnotationResult(
    status=AnnotationStatus.saved.name, description="the annotation was saved"
)
UPDATED_ANNOTATION_RESULT = AnnotationResult(
    status=AnnotationStatus.updated.name,
    description="the annotation was saved and sent to OFF",
)
MISSING_PRODUCT_RESULT = AnnotationResult(
    status=AnnotationStatus.error_missing_product.name,
    description="the product could not be found on OFF",
)
ALREADY_ANNOTATED_RESULT = AnnotationResult(
    status=AnnotationStatus.error_already_annotated.name,
    description="the insight has already been annotated",
)
UNKNOWN_INSIGHT_RESULT = AnnotationResult(
    status=AnnotationStatus.error_unknown_insight.name, description="unknown insight ID"
)
LATENT_INSIGHT_RESULT = AnnotationResult(
    status=AnnotationStatus.error_latent_insight.name,
    description="cannot annotate a latent insight",
)
DATA_REQUIRED_RESULT = AnnotationResult(
    status=AnnotationStatus.error_missing_data.name,
    description="annotation data is required as JSON in `data` field",
)


class InsightAnnotator(metaclass=abc.ABCMeta):
    def annotate(
        self,
        insight: ProductInsight,
        annotation: int,
        update: bool = True,
        data: Optional[Dict] = None,
        auth: Optional[OFFAuthentication] = None,
        automatic: bool = False,
    ) -> AnnotationResult:
        if insight.latent:
            return LATENT_INSIGHT_RESULT

        with db.atomic() as transaction:
            try:
                return self._annotate(
                    insight, annotation, update, data, auth, automatic
                )
            except Exception as e:
                transaction.rollback()
                raise e

    def _annotate(
        self,
        insight: ProductInsight,
        annotation: int,
        update: bool = True,
        data: Optional[Dict] = None,
        auth: Optional[OFFAuthentication] = None,
        automatic: bool = False,
    ) -> AnnotationResult:
        if self.is_data_required() and data is None:
            return DATA_REQUIRED_RESULT

        username: Optional[str] = None
        if auth is not None:
            username = auth.get_username()

        insight.username = username
        insight.annotation = annotation
        insight.completed_at = datetime.datetime.utcnow()

        if automatic:
            insight.automatic_processing = True

        insight.save()

        if annotation == 1 and update:
            return self.process_annotation(insight, data=data, auth=auth)

        return SAVED_ANNOTATION_RESULT

    @abc.abstractmethod
    def process_annotation(
        self,
        insight: ProductInsight,
        data: Optional[Dict] = None,
        auth: Optional[OFFAuthentication] = None,
    ) -> AnnotationResult:
        pass

    def is_data_required(self) -> bool:
        return False


class PackagerCodeAnnotator(InsightAnnotator):
    def process_annotation(
        self,
        insight: ProductInsight,
        data: Optional[Dict] = None,
        auth: Optional[OFFAuthentication] = None,
    ) -> AnnotationResult:
        emb_code: str = insight.value

        product = get_product(insight.barcode, ["emb_codes"])

        if product is None:
            return MISSING_PRODUCT_RESULT

        emb_codes_str: str = product.get("emb_codes", "")

        emb_codes: List[str] = []
        if emb_codes_str:
            emb_codes = emb_codes_str.split(",")

        if self.already_exists(emb_code, emb_codes):
            return ALREADY_ANNOTATED_RESULT

        emb_codes.append(emb_code)
        update_emb_codes(
            insight.barcode,
            emb_codes,
            server_domain=insight.server_domain,
            insight_id=insight.id,
            auth=auth,
        )
        return UPDATED_ANNOTATION_RESULT

    @staticmethod
    def already_exists(new_emb_code: str, emb_codes: List[str]) -> bool:
        emb_codes = [normalize_emb_code(emb_code) for emb_code in emb_codes]

        normalized_emb_code = normalize_emb_code(new_emb_code)

        if normalized_emb_code in emb_codes:
            return True

        return False


class LabelAnnotator(InsightAnnotator):
    def process_annotation(
        self,
        insight: ProductInsight,
        data: Optional[Dict] = None,
        auth: Optional[OFFAuthentication] = None,
    ) -> AnnotationResult:
        product = get_product(insight.barcode, ["labels_tags"])

        if product is None:
            return MISSING_PRODUCT_RESULT

        labels_tags: List[str] = product.get("labels_tags") or []

        if insight.value_tag in labels_tags:
            return ALREADY_ANNOTATED_RESULT

        add_label_tag(
            insight.barcode,
            insight.value_tag,
            insight_id=insight.id,
            server_domain=insight.server_domain,
            auth=auth,
        )

        return UPDATED_ANNOTATION_RESULT


class IngredientSpellcheckAnnotator(InsightAnnotator):
    def process_annotation(
        self,
        insight: ProductInsight,
        data: Optional[Dict] = None,
        auth: Optional[OFFAuthentication] = None,
    ) -> AnnotationResult:
        barcode = insight.barcode
        lang = insight.data["lang"]
        field_name = "ingredients_text_{}".format(lang)
        product = get_product(barcode, [field_name])

        if product is None:
            return MISSING_PRODUCT_RESULT

        original_ingredients = insight.data["text"]
        corrected = insight.data["corrected"]
        expected_ingredients = product.get(field_name)

        if expected_ingredients != original_ingredients:
            logger.warning(
                "ingredients have changed since spellcheck insight "
                "creation (product {})".format(barcode)
            )
            return AnnotationResult(
                status=AnnotationStatus.error_updated_product.name,
                description="the ingredient list has been updated since spellcheck",
            )

        save_ingredients(
            barcode,
            corrected,
            lang=lang,
            insight_id=insight.id,
            auth=auth,
        )
        return UPDATED_ANNOTATION_RESULT


class CategoryAnnotator(InsightAnnotator):
    def process_annotation(
        self,
        insight: ProductInsight,
        data: Optional[Dict] = None,
        auth: Optional[OFFAuthentication] = None,
    ) -> AnnotationResult:
        product = get_product(insight.barcode, ["categories_tags"])

        if product is None:
            return MISSING_PRODUCT_RESULT

        categories_tags: List[str] = product.get("categories_tags") or []

        if insight.value_tag in categories_tags:
            return ALREADY_ANNOTATED_RESULT

        category_tag = insight.value_tag
        add_category(
            insight.barcode,
            category_tag,
            insight_id=insight.id,
            server_domain=insight.server_domain,
            auth=auth,
        )

        return UPDATED_ANNOTATION_RESULT


class ProductWeightAnnotator(InsightAnnotator):
    def process_annotation(
        self,
        insight: ProductInsight,
        data: Optional[Dict] = None,
        auth: Optional[OFFAuthentication] = None,
    ) -> AnnotationResult:
        product = get_product(insight.barcode, ["quantity"])

        if product is None:
            return MISSING_PRODUCT_RESULT

        quantity: Optional[str] = product.get("quantity") or None

        if quantity is not None:
            return ALREADY_ANNOTATED_RESULT

        update_quantity(
            insight.barcode,
            insight.value,
            insight_id=insight.id,
            server_domain=insight.server_domain,
            auth=auth,
        )

        return UPDATED_ANNOTATION_RESULT


class ExpirationDateAnnotator(InsightAnnotator):
    def process_annotation(
        self,
        insight: ProductInsight,
        data: Optional[Dict] = None,
        auth: Optional[OFFAuthentication] = None,
    ) -> AnnotationResult:
        product = get_product(insight.barcode, ["expiration_date"])

        if product is None:
            return MISSING_PRODUCT_RESULT

        current_expiration_date = product.get("expiration_date") or None

        if current_expiration_date:
            return ALREADY_ANNOTATED_RESULT

        update_expiration_date(
            insight.barcode,
            insight.value,
            insight_id=insight.id,
            server_domain=insight.server_domain,
            auth=auth,
        )
        return UPDATED_ANNOTATION_RESULT


class BrandAnnotator(InsightAnnotator):
    def process_annotation(
        self,
        insight: ProductInsight,
        data: Optional[Dict] = None,
        auth: Optional[OFFAuthentication] = None,
    ) -> AnnotationResult:
        product = get_product(insight.barcode, ["brands_tags"])

        if product is None:
            return MISSING_PRODUCT_RESULT

        add_brand(
            insight.barcode,
            insight.value,
            insight_id=insight.id,
            server_domain=insight.server_domain,
            auth=auth,
        )
        return UPDATED_ANNOTATION_RESULT


class StoreAnnotator(InsightAnnotator):
    def process_annotation(
        self,
        insight: ProductInsight,
        data: Optional[Dict] = None,
        auth: Optional[OFFAuthentication] = None,
    ) -> AnnotationResult:
        product = get_product(insight.barcode, ["stores_tags"])

        if product is None:
            return MISSING_PRODUCT_RESULT

        stores_tags: List[str] = product.get("stores_tags") or []

        if insight.value_tag in stores_tags:
            return ALREADY_ANNOTATED_RESULT

        add_store(
            insight.barcode,
            insight.value,
            insight_id=insight.id,
            server_domain=insight.server_domain,
            auth=auth,
        )
        return UPDATED_ANNOTATION_RESULT


class PackagingAnnotator(InsightAnnotator):
    def process_annotation(
        self,
        insight: ProductInsight,
        data: Optional[Dict] = None,
        auth: Optional[OFFAuthentication] = None,
    ) -> AnnotationResult:
        packaging_tag: str = insight.value_tag

        product = get_product(insight.barcode, ["packaging_tags"])

        if product is None:
            return MISSING_PRODUCT_RESULT

        packaging_tags: List[str] = product.get("packaging_tags") or []

        if packaging_tag in packaging_tags:
            return ALREADY_ANNOTATED_RESULT

        add_packaging(
            insight.barcode,
            insight.value,
            insight_id=insight.id,
            server_domain=insight.server_domain,
            auth=auth,
        )
        return UPDATED_ANNOTATION_RESULT


class NutritionImageAnnotator(InsightAnnotator):
    def process_annotation(
        self,
        insight: ProductInsight,
        data: Optional[Dict] = None,
        auth: Optional[OFFAuthentication] = None,
    ) -> AnnotationResult:
        product = get_product(insight.barcode, ["code"])

        if product is None:
            return MISSING_PRODUCT_RESULT

        image_id = get_image_id(insight.source_image or "")

        if not image_id:
            return AnnotationResult(
                status="error_invalid_image",
                description="the image is invalid",
            )
        image_key = "nutrition_{}".format(insight.value_tag)
        select_rotate_image(
            barcode=insight.barcode,
            image_id=image_id,
            image_key=image_key,
            rotate=insight.data.get("rotation"),
            server_domain=insight.server_domain,
            auth=auth,
        )
        return UPDATED_ANNOTATION_RESULT


class NutritionTableStructureAnnotator(InsightAnnotator):
    def process_annotation(
        self,
        insight: ProductInsight,
        data: Optional[Dict] = None,
        auth: Optional[OFFAuthentication] = None,
    ) -> AnnotationResult:
        insight.data["annotation"] = data
        insight.save()

        return SAVED_ANNOTATION_RESULT

    def is_data_required(self):
        return True


class InsightAnnotatorFactory:
    mapping = {
        InsightType.ingredient_spellcheck.name: IngredientSpellcheckAnnotator(),
        InsightType.packager_code.name: PackagerCodeAnnotator(),
        InsightType.label.name: LabelAnnotator(),
        InsightType.category.name: CategoryAnnotator(),
        InsightType.product_weight.name: ProductWeightAnnotator(),
        InsightType.expiration_date.name: ExpirationDateAnnotator(),
        InsightType.brand.name: BrandAnnotator(),
        InsightType.store.name: StoreAnnotator(),
        InsightType.packaging.name: PackagingAnnotator(),
        InsightType.nutrition_image.name: NutritionImageAnnotator(),
        InsightType.nutrition_table_structure.name: NutritionTableStructureAnnotator(),
    }

    @classmethod
    def get(cls, identifier: str) -> InsightAnnotator:
        if identifier not in cls.mapping:
            raise ValueError("unknown annotator: {}".format(identifier))

        return cls.mapping[identifier]


class InvalidInsight(Exception):
    pass


def is_automatically_processable(
    barcode: str, source_image: Optional[str], max_timedelta: datetime.timedelta
) -> bool:
    if not source_image:
        return False

    image_path = pathlib.Path(source_image)
    image_id = image_path.stem

    if not image_id.isdigit():
        return False

    product = get_product(barcode, projection=["images"])

    if product is None:
        logger.debug("Missing product: {}".format(barcode))
        raise InvalidInsight()

    if "images" not in product:
        logger.debug("No images for product {}".format(barcode))
        raise InvalidInsight()

    product_images = product["images"]

    if image_id not in product_images:
        logger.debug("Missing image for product {}, ID: {}".format(barcode, image_id))
        raise InvalidInsight()

    if is_recent_image(product_images, image_id, max_timedelta):
        return True

    if is_selected_image(product_images, image_id):
        return True

    return False


def is_selected_image(product_images: JSONType, image_id: str) -> bool:
    for key_prefix in ("nutrition", "front", "ingredients"):
        for key, image in product_images.items():
            if key.startswith(key_prefix):
                if image["imgid"] == image_id:
                    logger.debug(
                        "Image {} is a selected image for "
                        "'{}'".format(image_id, key_prefix)
                    )
                    return True

    return False


def is_recent_image(
    product_images: JSONType, image_id: str, max_timedelta: datetime.timedelta
) -> bool:
    upload_datetimes = []
    insight_image_upload_datetime: Optional[datetime.datetime] = None

    for key, image_meta in product_images.items():
        if not key.isdigit():
            continue

        upload_datetime = datetime.datetime.utcfromtimestamp(
            int(image_meta["uploaded_t"])
        )
        if key == image_id:
            insight_image_upload_datetime = upload_datetime
        else:
            upload_datetimes.append(upload_datetime)

    if not upload_datetimes:
        logger.debug("No other images")
        return True

    if insight_image_upload_datetime is None:
        raise ValueError("Image with ID {} not found".format(image_id))

    else:
        for upload_datetime in upload_datetimes:
            if upload_datetime - insight_image_upload_datetime > max_timedelta:
                logger.debug(
                    "More recent image: {} > {}".format(
                        upload_datetime, insight_image_upload_datetime
                    )
                )
                return False

        sorted_datetimes = [
            str(x)
            for x in sorted(set(x.date() for x in upload_datetimes), reverse=True)
        ]
        logger.debug(
            "All images were uploaded the same day or before the target "
            "image:\n{} >= {}".format(
                insight_image_upload_datetime.date(), ", ".join(sorted_datetimes)
            )
        )
        return True

    logger.debug(
        "More recent images: {} < {}".format(
            insight_image_upload_datetime.date(),
            max(x.date() for x in upload_datetimes),
        )
    )
    return False

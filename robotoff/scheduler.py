import datetime
from typing import Dict, Set

from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.blocking import BlockingScheduler

from robotoff.insights._enum import InsightType
from robotoff.insights.annotate import InsightAnnotatorFactory
from robotoff.insights.importer import InsightImporterFactory, InsightImporter
from robotoff.models import ProductInsight, db
from robotoff.products import has_dataset_changed, fetch_dataset
from robotoff.utils import get_logger

logger = get_logger(__name__)

NEED_VALIDATION_INSIGHTS: Set[str] = set()


def process_insights():
    processed = 0
    with db:
        with db.atomic():
            for insight in (ProductInsight.select()
                                          .where(ProductInsight.annotation.is_null(),
                                                 ProductInsight.process_after.is_null(False),
                                                 ProductInsight.process_after <= datetime.datetime.utcnow())
                                          .iterator()):
                insight.annotation = 1
                insight.completed_at = datetime.datetime.utcnow()
                insight.save()

                annotator = InsightAnnotatorFactory.get(insight.type)
                logger.info("Annotating insight {} (product: {})".format(insight.id, insight.barcode))
                annotator.annotate(insight, 1, update=True)
                processed += 1

    logger.info("{} insights processed".format(processed))


def mark_insights():
    importers: Dict[str, InsightImporter] = {
        insight_type.name: InsightImporterFactory.create(insight_type.name,
                                                         None)
        for insight_type in InsightType
        if insight_type.name in InsightImporterFactory.importers
    }

    marked = 0
    with db:
        with db.atomic():
            for insight in (ProductInsight.select()
                                          .where(ProductInsight.process_after.is_null())
                                          .iterator()):
                if insight.id in NEED_VALIDATION_INSIGHTS:
                    continue

                importer = importers.get(insight.type)

                if importer is None:
                    continue

                if not importer.need_validation(insight):
                    logger.info("Marking insight {} as processable automatically "
                                "(product: {})".format(insight.id, insight.barcode))
                    insight.process_after = datetime.datetime.utcnow()
                    insight.save()
                    marked += 1
                else:
                    NEED_VALIDATION_INSIGHTS.add(insight.id)

    logger.info("{} insights marked".format(marked))


def download_product_dataset():
    logger.info("Downloading new version of product dataset")

    if has_dataset_changed():
        fetch_dataset()


def run():
    scheduler = BlockingScheduler()
    scheduler.add_executor(ThreadPoolExecutor(20))
    scheduler.add_jobstore(MemoryJobStore())
    scheduler.add_job(process_insights, 'interval', minutes=2, max_instances=1, jitter=20)
    scheduler.add_job(mark_insights, 'interval', minutes=2, max_instances=1, jitter=20)
    scheduler.add_job(download_product_dataset, 'cron', day='*', hour='3', max_instances=1)
    scheduler.start()

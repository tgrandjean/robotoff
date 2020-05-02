from typing import List, Dict, Union

from robotoff.insights.ocr.dataclass import OCRResult, ImageOrientation


def get_rotation_angle_from_orientation(image_orientation: ImageOrientation) -> int:
    if image_orientation == ImageOrientation.up:
        return 0
    elif image_orientation == ImageOrientation.left:
        return 90
    elif image_orientation == ImageOrientation.right:
        return 270
    elif image_orientation == ImageOrientation.down:
        return 180
    else:
        raise ValueError("unknown image orientation: {}".format(image_orientation))


def find_image_orientation(ocr_result: Union[OCRResult, str]) -> List[Dict]:
    if isinstance(ocr_result, str):
        return []

    orientation_result = ocr_result.get_orientation()

    if orientation_result is None:
        return []

    insight = orientation_result.to_json()
    insight["rotation"] = get_rotation_angle_from_orientation(
        orientation_result.orientation
    )
    return [insight]

import json
import sys
import time
import cv2
import jsonpointer
import numpy as np

from taggridscanner.aux.config import get_roi_aspect_ratio, set_roi, store_config
from taggridscanner.aux.http_json_poster import HttpJsonPoster
from taggridscanner.aux.newline_detector import NewlineDetector
from taggridscanner.aux.notification_manager import NotificationManager
from taggridscanner.pipeline.condense_tiles import CondenseTiles
from taggridscanner.pipeline.crop_tile_cells import CropTileCells
from taggridscanner.pipeline.detect_tags import DetectTags
from taggridscanner.pipeline.draw_grid import DrawGrid
from taggridscanner.pipeline.draw_roi_editor import DrawROIEditor
from taggridscanner.pipeline.extract_roi import ExtractROI
from taggridscanner.pipeline.image_source import ImageSource
from taggridscanner.pipeline.preprocess import Preprocess
from taggridscanner.pipeline.remove_gaps import RemoveGaps
from taggridscanner.pipeline.threshold import Threshold
from taggridscanner.pipeline.upscale import Upscale
from taggridscanner.pipeline.view_image import ViewImage

from taggridscanner.aux.threading import ThreadSafeContainer, WorkerThread
from taggridscanner.aux.utils import (
    abs_corners_to_rel_corners,
    rel_corners_to_abs_corners,
    Functor,
    Timeout,
    create_scan_result_transformer,
)


def clamp_points(points, img_shape):
    for idx in range(0, 4):
        points[idx][0] = max(0, min(points[idx][0], img_shape[1]))
        points[idx][1] = max(0, min(points[idx][1], img_shape[0]))


def create_notifier(notify_config):
    notifiers = []
    if notify_config["stdout"]:
        notifiers.append(lambda s: print(s, file=sys.stdout))
    if notify_config["stderr"]:
        notifiers.append(lambda s: print(s, file=sys.stderr))
    if notify_config["remote"]:
        http_json_poster = HttpJsonPoster(notify_config["url"])
        notifiers.append(lambda s: http_json_poster.request_post(s))

    notification_manager = NotificationManager(
        notifiers, notify_config["interval"] if notify_config["repeat"] else None
    )

    template = notify_config["template"]
    assign_to = notify_config["assignTo"]

    scan_result_transformer = create_scan_result_transformer(notify_config)

    def notify(new_tags):
        new_tags = scan_result_transformer(new_tags)
        notification_obj = jsonpointer.set_pointer(template, assign_to, new_tags, False)
        notification = json.dumps(notification_obj)
        notification_manager.notify(notification)

    return notify


def done(raw_config, config_path, rel_corners):
    print(json.dumps(rel_corners.tolist()), file=sys.stderr)

    print(
        "Press ENTER to save ROI to config file: {}".format(config_path),
        file=sys.stderr,
    )
    print("Press any other key to abort.", file=sys.stderr)
    key = cv2.waitKey()

    if key == 13:  # <ENTER>
        print("Saving ROI to: {}".format(config_path), file=sys.stderr)
        modified_raw_config = set_roi(raw_config, rel_corners)
        store_config(modified_raw_config, config_path)
    else:
        print("Aborting.", file=sys.stderr)


class ROIWorker(Functor):
    def __init__(self, config_with_defaults):
        super().__init__(lambda: self.work())
        self.config_with_defaults = config_with_defaults
        self.image_source = ImageSource.create_from_config(self.config_with_defaults)
        self.preprocess = Preprocess.create_from_config(self.config_with_defaults)

        self.h, self.w = self.image_source.size

        rel_roi_vertices = self.config_with_defaults["dimensions"]["roi"]
        self.idx = 0
        self.vertices = rel_corners_to_abs_corners(rel_roi_vertices, (self.h, self.w))

        self.draw_roi_editor = DrawROIEditor(
            vertices=self.vertices, active_vertex=self.idx
        )

        self.extract_roi = ExtractROI(
            target_aspect_ratio=get_roi_aspect_ratio(self.config_with_defaults),
            rel_corners=abs_corners_to_rel_corners(self.vertices, (self.h, self.w)),
        )

        grid_shape = self.config_with_defaults["dimensions"]["grid"]
        tag_shape = self.config_with_defaults["dimensions"]["tile"]
        rel_gap = self.config_with_defaults["dimensions"]["gap"]
        crop_factors = self.config_with_defaults["dimensions"]["crop"]
        tags = self.config_with_defaults["tags"]

        self.remove_gaps = RemoveGaps(grid_shape, tag_shape, rel_gap)
        self.crop_tile_pixels = CropTileCells(grid_shape, tag_shape, crop_factors)
        self.condense_tiles = CondenseTiles(grid_shape, tag_shape)
        self.threshold = Threshold(grid_shape, tag_shape)
        self.detect_tags = DetectTags(
            grid_shape, tag_shape, tags, detect_rotations=True
        )
        self.upscale = Upscale(10)
        self.draw_grid = DrawGrid(grid_shape, tag_shape, crop_factors)
        self.draw_grid_no_crop = DrawGrid(grid_shape, tag_shape, (1, 1))

        self.last_tag_data = self.detect_tags.create_empty_tags()

        self.__key = ThreadSafeContainer()
        self.__compute_visualization = ThreadSafeContainer(True)

    @property
    def key(self):
        return self.__key

    @property
    def compute_visualization(self):
        return self.__compute_visualization

    def default_vertices(self):
        return np.array(
            [
                [self.w / 4.0, self.h / 4.0],
                [3.0 * self.w / 4.0, self.h / 4.0],
                [3.0 * self.w / 4.0, 3.0 * self.h / 4.0],
                [self.w / 4.0, 3.0 * self.h / 4.0],
            ]
        )

    def work(self):
        start_ts = time.perf_counter()

        try:
            key = self.key.retrieve_nowait()
            vert_step_small = 0.25
            vert_step_big = 10.0
            if key == -1:
                pass
            elif key == 119:  # w
                self.vertices[self.idx][1] -= vert_step_small
            elif key == 97:  # a
                self.vertices[self.idx][0] -= vert_step_small
            elif key == 115:  # s
                self.vertices[self.idx][1] += vert_step_small
            elif key == 100:  # d
                self.vertices[self.idx][0] += vert_step_small
            elif key == 87:  # W
                self.vertices[self.idx][1] -= vert_step_big
            elif key == 65:  # A
                self.vertices[self.idx][0] -= vert_step_big
            elif key == 83:  # S
                self.vertices[self.idx][1] += vert_step_big
            elif key == 68:  # D
                self.vertices[self.idx][0] += vert_step_big
            elif key == 32:  # <SPACE>
                self.idx = (self.idx + 1) % 4
            elif key == 99:  # c
                self.vertices = self.default_vertices()
        except ThreadSafeContainer.Empty:
            pass

        clamp_points(self.vertices, (self.h, self.w))
        self.draw_roi_editor.active_vertex = self.idx
        self.draw_roi_editor.vertices = self.vertices

        src = self.image_source.read()
        preprocessed = self.preprocess(src)

        self.extract_roi.rel_corners = abs_corners_to_rel_corners(
            self.vertices, (self.h, self.w)
        )
        extracted_roi = self.extract_roi(preprocessed)
        gaps_removed = self.remove_gaps(extracted_roi)
        cropped = self.crop_tile_pixels(gaps_removed)
        condensed = self.condense_tiles(cropped)
        thresholded = self.threshold(condensed)
        tag_data = self.detect_tags(thresholded)

        if not np.array_equal(self.last_tag_data, tag_data):
            self.last_tag_data = tag_data

        rel_corners = abs_corners_to_rel_corners(self.vertices, (self.h, self.w))

        try:
            if self.compute_visualization.get_nowait():
                roi_editor_img = self.draw_roi_editor(preprocessed)
                gaps_removed_with_grid = self.draw_grid(gaps_removed)
                cropped_with_grid = self.draw_grid_no_crop(cropped)
                condensed_with_grid = self.draw_grid_no_crop(self.upscale(condensed))
                thresholded_with_grid = self.draw_grid_no_crop(
                    self.upscale(thresholded)
                )
                viz = (
                    roi_editor_img,
                    extracted_roi,
                    gaps_removed_with_grid,
                    cropped_with_grid,
                    condensed_with_grid,
                    thresholded_with_grid,
                )
            else:
                viz = None
        except ThreadSafeContainer.Empty:
            viz = None

        end_ts = time.perf_counter()
        rate = 1.0 / (end_ts - start_ts)
        # print("max. {:.1f} detections per second".format(rate), file=sys.stderr)

        return (
            tag_data,
            rel_corners,
            viz,
        )


def scan(args):
    config_with_defaults = args["config-with-defaults"]

    view_roi_editor = ViewImage("select roi")
    view_extracted_roi = ViewImage("extracted roi")
    view_roi_without_gaps = ViewImage("gaps removed")
    view_cropped_tile_cells = ViewImage("cropped tile pixels")
    view_condensed_cells = ViewImage("condensed")
    view_thresholded = ViewImage("thresholded")
    all_viewers = [
        view_roi_editor,
        view_extracted_roi,
        view_roi_without_gaps,
        view_cropped_tile_cells,
        view_condensed_cells,
        view_thresholded,
    ]

    rel_corners = None
    max_fps = 60
    has_window = False

    newline_detector = NewlineDetector()
    newline_detector.start()
    print("Press ENTER to hide/show the UI.", file=sys.stderr)

    auto_hide_timeout = Timeout(args["auto_hide"])

    roi_worker = ROIWorker(config_with_defaults)
    producer = WorkerThread(roi_worker)
    producer.rate_limit = 4
    producer.start()
    producer.result.wait()

    last_tag_data = None
    notify = create_notifier(config_with_defaults["notify"])

    while True:
        frame_start_ts = time.perf_counter()

        try:
            (tag_data, rel_corners, viz) = producer.result.retrieve_nowait()

            if not np.array_equal(last_tag_data, tag_data):
                notify(tag_data.tolist())
                last_tag_data = tag_data

            if viz is None:
                for view_image in all_viewers:
                    view_image.hide()
                cv2.pollKey()
                has_window = False
            else:
                (
                    roi_editor_img,
                    extracted_roi,
                    gaps_removed_with_grid,
                    cropped_with_grid,
                    condensed_with_grid,
                    thresholded_with_grid,
                ) = viz

                view_roi_editor(roi_editor_img)
                view_extracted_roi(extracted_roi)
                view_roi_without_gaps(gaps_removed_with_grid)
                view_cropped_tile_cells(cropped_with_grid)
                view_condensed_cells(condensed_with_grid)
                view_thresholded(thresholded_with_grid)
                has_window = True

        except ThreadSafeContainer.Empty:
            pass

        frame_end_ts = time.perf_counter()
        frame_time_left = max(0.0, 1.0 / max_fps - (frame_end_ts - frame_start_ts))

        try:
            newline_detector.result.retrieve_nowait()
            auto_hide_timeout.reset()
            with roi_worker.compute_visualization.condition:
                try:
                    show_ui = not roi_worker.compute_visualization.get_nowait()
                except ThreadSafeContainer.Empty:
                    show_ui = False
                roi_worker.compute_visualization.set(show_ui)
        except ThreadSafeContainer.Empty:
            pass

        if auto_hide_timeout.is_up():
            auto_hide_timeout.reset()
            roi_worker.compute_visualization.set(False)

        try:
            if not roi_worker.compute_visualization.get_nowait():
                for view_image in all_viewers:
                    view_image.hide()
                cv2.pollKey()
        except ThreadSafeContainer.Empty:
            pass

        if has_window:
            ms_to_wait_for_key = max(1, int(1000 * frame_time_left))
            key = cv2.waitKey(ms_to_wait_for_key)
            if key == -1:
                pass
            else:
                auto_hide_timeout.reset()
                if key == 27:  # <ESC>
                    print("Aborting.", file=sys.stderr)
                    sys.exit(1)
                elif key == 13:  # <ENTER>
                    if rel_corners is not None:
                        done(
                            args["raw-config"],
                            args["config-path"],
                            rel_corners,
                        )
                        sys.exit(0)
                roi_worker.key.set(key)
        else:
            time.sleep(frame_time_left)

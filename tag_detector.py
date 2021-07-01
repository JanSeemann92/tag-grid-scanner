import cv2
import math
import numpy as np


def tiles_to_image(tiles, max_value=255, scale_factor=1):
    img = np.zeros(
        (tiles.shape[0] * tiles.shape[2], tiles.shape[1] * tiles.shape[3]),
        dtype=np.uint8,
    )
    for grid_y in range(tiles.shape[0]):
        for grid_x in range(tiles.shape[1]):
            img[
                grid_y * tiles.shape[2] : (grid_y + 1) * tiles.shape[2],
                grid_x * tiles.shape[3] : (grid_x + 1) * tiles.shape[3],
            ] = tiles[grid_y][grid_x]

    img *= max_value

    if scale_factor != 1.0:
        img = cv2.resize(
            img,
            None,
            fx=scale_factor,
            fy=scale_factor,
            interpolation=cv2.INTER_NEAREST,
        )

    return img


class TagDetector:
    def __init__(self, grid_size, tag_size, rel_gaps, tags):
        self.grid_size = grid_size
        self.rel_gaps = rel_gaps
        self.__tag_size = tag_size
        self.__tags = tags
        self.__tag_dict = self.create_tag_dict(tags)

    @property
    def tag_size(self):
        return self.__tag_size

    @tag_size.setter
    def tag_size(self, s):
        self.__tag_size = s
        self.__tag_dict = self.create_tag_dict(self.__tags)

    @property
    def tags(self):
        return self.__tags

    @tags.setter
    def tags(self, t):
        self.__tags = t
        self.__tag_dict = self.create_tag_dict(self.__tags)

    def extract_tiles(self, img):
        tiles = np.zeros((self.grid_size + self.tag_size), dtype=np.uint8)
        for grid_y in range(self.grid_size[0]):
            for grid_x in range(self.grid_size[1]):
                window = self.tile_window(img, grid_x, grid_y)
                tile = self.reduce_tile(window)
                tiles[grid_y, grid_x] = tile
        return tiles

    def detect_tags(self, tiles):
        detected_tags = np.zeros((tiles.shape[0], tiles.shape[1]), dtype=np.int32)
        for grid_y in range(self.grid_size[0]):
            for grid_x in range(self.grid_size[1]):
                tile = tiles[grid_y, grid_x]
                tile_id = self.__tag_dict.get(self.np_tag_to_int(tile), -1)
                detected_tags[grid_y, grid_x] = tile_id
        return detected_tags

    def create_empty_tags(self):
        return np.full(self.grid_size, -1, dtype=np.int32)

    def string_tag_to_np_tag(self, string_tag):
        return np.fromstring(
            ",".join(list(string_tag)),
            np.uint8,
            self.tag_size[0] * self.tag_size[1],
            ",",
        ).reshape(self.tag_size)

    def np_tag_to_string_tag(self, np_tag):
        return "".join(
            str(e) for e in list(np_tag.reshape(self.tag_size[0] * self.tag_size[1]))
        )

    def string_tag_to_int(self, string_tag):
        return int(string_tag, 2)

    def np_tag_to_int(self, np_tag):
        tag_size_linear = self.tag_size[0] * self.tag_size[1]
        np_tag_linear = np_tag.reshape(tag_size_linear)
        mask = 1 << tag_size_linear
        int_tag = 0
        for bit in np_tag_linear:
            mask >>= 1
            if bit:
                int_tag |= mask
        return int_tag

    def create_tag_dict(self, string_tags):
        tag_dict = {}
        for idx, string_tag in enumerate(string_tags):
            np_tag = self.string_tag_to_np_tag(string_tag)
            tag_dict[self.np_tag_to_int(np_tag)] = idx
            np_tag = np.rot90(np_tag)
            tag_dict[self.np_tag_to_int(np_tag)] = idx
            np_tag = np.rot90(np_tag)
            tag_dict[self.np_tag_to_int(np_tag)] = idx
            np_tag = np.rot90(np_tag)
            tag_dict[self.np_tag_to_int(np_tag)] = idx
        return tag_dict

    def tile_window(self, img, grid_x, grid_y):
        gap_height = img.shape[0] * self.rel_gaps[0]
        img_height_with_added_gap = img.shape[0] + gap_height
        tile_height_with_gap = img_height_with_added_gap / self.grid_size[0]
        tile_height = tile_height_with_gap - gap_height
        y_start = min(math.floor(grid_y * tile_height_with_gap), img.shape[0] - 1)
        y_end = min(
            math.floor(grid_y * tile_height_with_gap + tile_height), img.shape[0] - 1
        )

        gap_width = img.shape[1] * self.rel_gaps[1]
        img_width_with_added_gap = img.shape[1] + gap_width
        tile_width_with_gap = img_width_with_added_gap / self.grid_size[1]
        tile_width = tile_width_with_gap - gap_width
        x_start = min(math.floor(grid_x * tile_width_with_gap), img.shape[1] - 1)
        x_end = min(
            math.floor(grid_x * tile_width_with_gap + tile_width), img.shape[1] - 1
        )

        window = img[y_start:y_end, x_start:x_end]

        return window

    def reduce_tile(self, tile_img_gray):
        tile_small = cv2.resize(
            tile_img_gray,
            self.tag_size,
            interpolation=cv2.INTER_AREA,
        )
        ret, tile_small_bw = cv2.threshold(
            tile_small, 0, 1, cv2.THRESH_BINARY + cv2.THRESH_OTSU
        )
        return tile_small_bw
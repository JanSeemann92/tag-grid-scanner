import sys
import cv2
from taggridscanner.pipeline.image_source import ImageSource
from taggridscanner.pipeline.view_image import ViewImage


def snapshot(args):
    config_with_defaults = args["config-with-defaults"]
    camera_config = config_with_defaults["camera"]
    output_filename = args.get("OUTFILE", None)

    image_source = ImageSource.create_from_config(config_with_defaults)
    view_image = ViewImage("Snapshot")

    key = 0

    while key != 27 and key != ord("q"):
        print(
            "Press SPACE or ENTER to take a snapshot. Press ESC or q to quit.",
            file=sys.stderr,
        )
        while key != 27 and key != ord("q"):
            frame = image_source.read()
            view_image(frame)

            key = cv2.waitKey(1)
            if key == 32 or key == 13:
                if output_filename is not None:
                    print(
                        "Saving snapshot to {}".format(output_filename),
                        file=sys.stderr,
                    )
                    cv2.imwrite(camera_config["filename"], frame)
                else:
                    print(
                        "No output filename specified. Not saving.",
                        file=sys.stderr,
                    )
                print("Press ESC or q to quit. Press any other key to try again.")
                key = cv2.waitKey()
                break

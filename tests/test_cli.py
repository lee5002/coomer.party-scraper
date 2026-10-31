import unittest
from pathlib import Path
from unittest.mock import patch

from coomerscraper import __version__
from coomerscraper.__main__ import main


class CliVersionTests(unittest.TestCase):
    def test_startup_prints_package_version_once(self):
        arguments = (
            ['https://coomerfans.com/u/creator/service'],
            Path('.'),
            False,
            False,
            (None, None),
            False,
            1,
            None,
        )
        with patch('coomerscraper.__main__.get_arguments', return_value=arguments):
            with patch('coomerscraper.__main__.coom_main'):
                with patch('builtins.print') as print_output:
                    main()

        print_output.assert_called_once_with(
            f'Coomer Scraper v{__version__}',
            flush=True,
        )


if __name__ == '__main__':
    unittest.main()

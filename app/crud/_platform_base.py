

class CrudBase: #pylint: disable=too-few-public-methods
    """
    * Base Crud class
    """

    def __init__(self, db_session) -> None:
        self.db_session = db_session

        self.collection = None
 
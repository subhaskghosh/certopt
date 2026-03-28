SELECT
  firstname,
  lastname,
  city,
  state
FROM (
  SELECT
    *
  FROM PERSON
  LEFT JOIN ADDRESS
    ON PERSON.PERSONID = ADDRESS.PERSONID
) AS _subquery
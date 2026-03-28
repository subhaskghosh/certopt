SELECT
  firstname,
  lastname,
  city,
  state
FROM PERSON AS A
LEFT JOIN ADDRESS AS B
  ON a.personid = b.personid